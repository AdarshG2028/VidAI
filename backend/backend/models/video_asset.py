"""VideoAsset — durable per-video assets (Phase 10 foundation).

Promotes an Asset (backend/workers/media.py) from per-job/per-Result to
per-video and persistent: today whatever a capability produces lives only
in that job's Result row, so e.g. a transcript is recomputed by every job
that needs one, even for the same unmodified video. This table holds the
*current* asset of a given kind for a video -- keyed by (video_id, kind),
so a fresh run overwrites the prior one rather than accumulating history
(Result already keeps the job-scoped history; this is a cache, not an
archive).

Scoped narrowly for now: only transcribe writes/reads this table, to
cache transcripts across previews/re-runs/final renders of the same
video (see TranscribeWorker). latest_analysis_job_id's pointer-chasing
for video_analysis output is deliberately left as-is -- migrating it is a
separate, larger change not needed for this.
"""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base, JSONType, TimestampMixin


class VideoAsset(Base, TimestampMixin):
    __tablename__ = "video_assets"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)

    video_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    # Plain str, matching AssetKind (backend/workers/media.py) rather than
    # importing it -- this model is Setu's persistence layer, and pinning
    # it to one product's kind list would invert that layering.
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Room for a capability to attach structured output alongside its
    # storage object (e.g. a future embeddings capability's vector, or
    # segment offsets) without a second round trip through Storage.
    # Unused by transcript caching, which stores everything at `uri`.
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    __table_args__ = (
        # One current asset per (video, kind) -- a re-run replaces it,
        # doesn't add a row, which is what lets an upsert be the whole
        # write path (see VideoAssetRepository.upsert).
        sa.UniqueConstraint("video_id", "kind", name="uq_video_assets_video_kind"),
        sa.Index("ix_video_assets_video_id", "video_id"),
    )
