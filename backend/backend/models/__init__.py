"""SQLAlchemy ORM models.

Importing this package registers every model on ``Base.metadata``; Alembic's
autogenerate depends on that, so new models must be re-exported here.
"""

from backend.database.base import Base
from backend.models.conversation import Conversation
from backend.models.enums import (
    Decision,
    ExecutionStatus,
    JobStatus,
    MessageRole,
    OutboxStatus,
)
from backend.models.idempotency import IdempotencyKey
from backend.models.job import Job
from backend.models.message import Message
from backend.models.outbox import OutboxEvent
from backend.models.project import Project
from backend.models.project_job import ProjectJob
from backend.models.project_member import ProjectMember
from backend.models.proposal import Proposal, ProposalStatus
from backend.models.proposal_approval import ProposalApproval
from backend.models.result import Result
from backend.models.user_preference import UserPreference
from backend.models.video import Video
from backend.models.video_asset import VideoAsset
from backend.models.worker_execution import WorkerExecution

__all__ = [
    "Base",
    "Conversation",
    "Decision",
    "ExecutionStatus",
    "IdempotencyKey",
    "Job",
    "JobStatus",
    "Message",
    "MessageRole",
    "OutboxEvent",
    "OutboxStatus",
    "Project",
    "ProjectJob",
    "ProjectMember",
    "Proposal",
    "ProposalApproval",
    "ProposalStatus",
    "Result",
    "UserPreference",
    "Video",
    "VideoAsset",
    "WorkerExecution",
]
