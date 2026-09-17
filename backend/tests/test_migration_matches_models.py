"""Guards against the migration history drifting from the ORM models.

Compares the DDL Alembic emits in offline mode (the full migration chain,
baseline CREATE TABLEs plus any later ALTER TABLEs) against the DDL
SQLAlchemy generates from Base.metadata. Any missing column, constraint,
or index shows up as a diff.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from backend.models import Base

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _normalize(sql: str) -> str:
    """Collapse whitespace and drop trailing commas/semicolons for comparison."""
    sql = re.sub(r"\s+", " ", sql).strip().rstrip(";").strip()
    return sql.replace("( ", "(").replace(" )", ")").replace(", ", ",")


# Fragments that aren't a plain "column_name type ..." definition -- keyed by
# their own text (self-keyed) rather than a column name, since ALTER COLUMN
# never targets them and they're never individually replaced, only ever
# present-or-absent as a whole.
_CONSTRAINT_KEYWORDS = ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT")

# Every ALTER TABLE form the migration chain uses to modify a table after its
# initial CREATE TABLE, matched as one pattern so they're processed in the
# document order they actually run in -- a DROP CONSTRAINT and a later ADD
# CONSTRAINT of the same name only resolve correctly if applied in sequence,
# not as two independent, order-blind passes.
_ALTER_RE = re.compile(
    r"ALTER TABLE (?P<add_col_table>\w+) ADD COLUMN (?P<add_col_def>.+?);"
    r"|ALTER TABLE (?P<add_con_table>\w+) ADD CONSTRAINT (?P<add_con_name>\w+) "
    r"(?P<add_con_def>FOREIGN KEY.+?);"
    # CHECK added to an existing table. Needed because Alembic's
    # autogenerate never emits one -- it detects CHECK constraints only
    # inside CREATE TABLE -- so any CHECK on a pre-existing table is
    # hand-written, which is exactly the case most likely to drift.
    r"|ALTER TABLE (?P<add_chk_table>\w+) ADD CONSTRAINT (?P<add_chk_name>\w+) "
    r"(?P<add_chk_def>CHECK .+?);"
    r"|ALTER TABLE (?P<drop_con_table>\w+) DROP CONSTRAINT (?P<drop_con_name>\w+);"
    r"|ALTER TABLE (?P<alt_col_table>\w+) ALTER COLUMN (?P<alt_col_name>\w+) "
    r"SET NOT NULL;",
    re.DOTALL,
)


def _fragment_key(fragment: str, index: int) -> str:
    first_word = fragment.split(maxsplit=1)[0] if fragment else ""
    if first_word.upper() in _CONSTRAINT_KEYWORDS:
        return fragment
    return first_word or f"_unnamed{index}"


def _parse_create_tables(sql: str) -> dict[str, str]:
    """Extract {table_name: normalized CREATE TABLE body} from a SQL script,
    replaying every ALTER TABLE against the original CREATE TABLE so a
    column added, renamed-nullability, or constraint dropped/re-added later
    in the migration chain is reflected exactly once, correctly, rather than
    just accumulated as extra text."""
    tables: dict[str, dict[str, str]] = {}
    for match in re.finditer(
        r"CREATE TABLE (\w+) \((.*?)\n\)\s*;", sql, re.DOTALL | re.IGNORECASE
    ):
        name = match.group(1)
        if name == "alembic_version":  # Alembic's own bookkeeping table
            continue
        fragments = _normalize(match.group(2)).split(",")
        tables[name] = {
            _fragment_key(frag, i): frag for i, frag in enumerate(fragments)
        }

    for match in _ALTER_RE.finditer(sql):
        if match.group("add_col_table") is not None:
            name, col_def = match.group("add_col_table"), match.group("add_col_def")
            if name in tables:
                col_def = _normalize(col_def)
                tables[name][_fragment_key(col_def, len(tables[name]))] = col_def
        elif match.group("add_con_table") is not None:
            name = match.group("add_con_table")
            con_name, con_def = match.group("add_con_name"), match.group("add_con_def")
            if name in tables:
                tables[name][f"constraint:{con_name}"] = _normalize(con_def)
        elif match.group("add_chk_table") is not None:
            name = match.group("add_chk_table")
            con_name, con_def = match.group("add_chk_name"), match.group("add_chk_def")
            if name in tables:
                # Stored with its CONSTRAINT prefix, unlike the foreign-key
                # branch above: SQLAlchemy renders a *named* constraint
                # inline as "CONSTRAINT <name> CHECK (...)", and the two
                # sides only compare equal if this matches that spelling.
                tables[name][f"constraint:{con_name}"] = _normalize(
                    f"CONSTRAINT {con_name} {con_def}"
                )
        elif match.group("drop_con_table") is not None:
            name, con_name = match.group("drop_con_table"), match.group("drop_con_name")
            if name in tables:
                tables[name].pop(f"constraint:{con_name}", None)
        elif match.group("alt_col_table") is not None:
            name, col = match.group("alt_col_table"), match.group("alt_col_name")
            if name in tables and col in tables[name]:
                current = tables[name][col]
                if "NOT NULL" not in current:
                    tables[name][col] = f"{current} NOT NULL"

    return {name: ",".join(frags.values()) for name, frags in tables.items()}


def _parse_create_indexes(sql: str) -> dict[str, str]:
    indexes: dict[str, str] = {}
    for match in re.finditer(r"CREATE (?:UNIQUE )?INDEX (\w+) (.*?);", sql, re.DOTALL):
        indexes[match.group(1)] = _normalize(match.group(2))
    return indexes


@pytest.fixture(scope="module")
def migration_sql() -> str:
    """Alembic offline-mode DDL for the full migration history."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture(scope="module")
def model_sql() -> str:
    """DDL generated directly from the ORM metadata."""
    dialect = postgresql.dialect()
    parts: list[str] = []
    for table in Base.metadata.sorted_tables:
        parts.append(f"{sa.schema.CreateTable(table).compile(dialect=dialect)};")
        for index in table.indexes:
            parts.append(f"{sa.schema.CreateIndex(index).compile(dialect=dialect)};")
    return "\n".join(parts)


def test_same_tables(migration_sql: str, model_sql: str) -> None:
    migrated = set(_parse_create_tables(migration_sql))
    modeled = set(_parse_create_tables(model_sql))
    assert migrated == modeled, f"table set differs: {migrated ^ modeled}"


def test_same_table_definitions(migration_sql: str, model_sql: str) -> None:
    migrated = _parse_create_tables(migration_sql)
    modeled = _parse_create_tables(model_sql)

    for name, modeled_body in modeled.items():
        migrated_cols = set(migrated[name].split(","))
        modeled_cols = set(modeled_body.split(","))
        assert migrated_cols == modeled_cols, (
            f"{name} differs.\n"
            f"  only in migration: {sorted(migrated_cols - modeled_cols)}\n"
            f"  only in models:    {sorted(modeled_cols - migrated_cols)}"
        )


def test_same_indexes(migration_sql: str, model_sql: str) -> None:
    migrated = _parse_create_indexes(migration_sql)
    modeled = _parse_create_indexes(model_sql)
    assert set(migrated) == set(modeled), (
        f"index set differs: {set(migrated) ^ set(modeled)}"
    )
    for name, definition in modeled.items():
        assert migrated[name] == definition, (
            f"index {name} differs:\n  migration: {migrated[name]}\n"
            f"  models:    {definition}"
        )
