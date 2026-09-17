"""add videos name

Revision ID: 8ccc76a18ae4
Revises: d0720bea3a38
Create Date: 2026-07-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8ccc76a18ae4"
down_revision: str | None = "d0720bea3a38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("videos", "name")
