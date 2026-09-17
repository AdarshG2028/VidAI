"""Message — one turn in a Conversation.

Immutable once written (no updated_at -- TimestampMixin isn't used here on
purpose). sender_id is nullable because assistant (and future system)
messages have no user behind them.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base
from backend.models.enums import MessageRole


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # No users table yet (auth lands in Phase 8) -- plain UUID, no FK.
    sender_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)

    role: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # Set client-side (not just server_default) so two Messages created in
    # the same transaction -- the common case, a user turn and its assistant
    # reply committed together -- get distinct, correctly-ordered timestamps.
    # Postgres's now() is transaction start time, not statement time, so a
    # server-only default would give both rows the identical value.
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=sa.func.now(),
        nullable=False,
    )

    __table_args__ = (
        sa.CheckConstraint(
            f"role IN ({', '.join(repr(r.value) for r in MessageRole)})",
            name="ck_messages_role",
        ),
        # Retrieval is always "this conversation's messages, in order".
        sa.Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )
