"""Status enumerations shared across models and services.

Stored as VARCHAR with a CHECK constraint rather than native Postgres enums:
adding a status stays a plain migration instead of an ALTER TYPE.
"""

from enum import StrEnum


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"
    CANCELLED = "cancelled"

    @classmethod
    def terminal(cls) -> frozenset["JobStatus"]:
        return frozenset({cls.COMPLETED, cls.DEAD_LETTERED, cls.CANCELLED})


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


class ExecutionStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Decision(StrEnum):
    """A member's vote on a proposal (Phase 9a).

    Here rather than beside the policy functions because it is a stored
    column value, like every other enum in this module -- which also keeps
    repositories from having to import out of services/ to read a row.
    """

    APPROVE = "approve"
    REJECT = "reject"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    # Not produced by anything yet -- reserved for future events like
    # "Proposal Approved" / "Job Started" / "Export Completed" so those
    # don't require a schema change when they arrive.
    SYSTEM = "system"
