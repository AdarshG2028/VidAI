"""videos project_id not null

Revision ID: d0720bea3a38
Revises: a846d1fffbe6
Create Date: 2026-07-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d0720bea3a38"
down_revision: str | None = "a846d1fffbe6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A video with no project is no longer a representable state. Any
    # existing orphans (pre-dating this migration, when project_id was
    # optional) are deleted rather than backfilled -- there is no project
    # to correctly attribute them to, and this is a dev-only dataset.
    op.execute("DELETE FROM videos WHERE project_id IS NULL")

    op.alter_column("videos", "project_id", existing_type=sa.Uuid(), nullable=False)

    # ON DELETE SET NULL is unrepresentable once the column is NOT NULL --
    # replaced with CASCADE: deleting a project deletes its videos.
    op.drop_constraint("fk_videos_project_id_projects", "videos", type_="foreignkey")
    op.create_foreign_key(
        "fk_videos_project_id_projects",
        "videos",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_videos_project_id_projects", "videos", type_="foreignkey")
    op.create_foreign_key(
        "fk_videos_project_id_projects",
        "videos",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column("videos", "project_id", existing_type=sa.Uuid(), nullable=True)
