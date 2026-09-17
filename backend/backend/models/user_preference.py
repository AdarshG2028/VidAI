"""UserPreference — durable per-user editing preferences.

Table exists from Phase 2 so the read path (loaded before every planning
call, per the architecture doc) is already wired; nothing writes to it
until Phase 6's memory-update call. No created_at -- a row's identity is
just "the current preferences for this user", not an event log.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base


class UserPreference(Base):
    __tablename__ = "user_preferences"

    # No users table yet (auth lands in Phase 8) -- plain UUID, no FK.
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)

    preferred_platform: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    preferred_export_format: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    captions_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    preferred_resolution: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    subtitle_language: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )
