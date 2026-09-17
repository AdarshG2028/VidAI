"""Data access for ProjectMember. No business logic — that lives in services/."""

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import ProjectMember

OWNER = "owner"
MEMBER = "member"

# An invitation, not yet accepted. Stored as a row so no invites table is
# needed -- the doc's "one new table" holds -- but deliberately NOT a
# role that grants access: being invited to a room is not the same as
# being in it, and a /join endpoint that did nothing would make accepting
# meaningless.
INVITED = "invited"

# The roles the membership guard lets through.
_ADMITTED = (OWNER, MEMBER)


class ProjectMemberRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_member(self, project_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """The membership guard's one question, kept as a single indexed
        primary-key lookup rather than loading the row."""
        result = await self._session.execute(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none() in _ADMITTED

    async def get_role(self, project_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
        result = await self._session.execute(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_project(self, project_id: uuid.UUID) -> list[ProjectMember]:
        result = await self._session.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.joined_at)
        )
        return list(result.scalars().all())

    async def count_active(self, project_id: uuid.UUID) -> int:
        """How many people can actually vote (Phase 9a).

        Excludes outstanding invitations, using the same `_ADMITTED` set
        the membership guard does. Counting them would make `team`
        unanimity unreachable the moment anyone is invited: the room could
        never execute anything until every invitee got round to accepting.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(ProjectMember)
            .where(
                ProjectMember.project_id == project_id,
                ProjectMember.role.in_(_ADMITTED),
            )
        )
        return result.scalar_one()

    async def active_user_ids(self, project_id: uuid.UUID) -> set[uuid.UUID]:
        """Who may vote. Used to drop votes cast by members who have since
        left, so a departed member cannot hold a decision hostage."""
        result = await self._session.execute(
            select(ProjectMember.user_id).where(
                ProjectMember.project_id == project_id,
                ProjectMember.role.in_(_ADMITTED),
            )
        )
        return set(result.scalars().all())

    async def list_project_ids_for(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        """Which rooms this user is in — served by ix_project_members_user_id."""
        result = await self._session.execute(
            select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
        )
        return list(result.scalars().all())

    async def accept_invitation(self, project_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Turn this user's invitation into membership. False if they had
        none -- which is what stops /join being a way to walk into any
        room whose id you happen to know."""
        result = await self._session.execute(
            update(ProjectMember)
            .where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
                ProjectMember.role == INVITED,
            )
            .values(role=MEMBER)
        )
        return result.rowcount > 0

    async def add(
        self, project_id: uuid.UUID, user_id: uuid.UUID, *, role: str = INVITED
    ) -> None:
        """Idempotent by design.

        Inviting someone already in the room, or a client retrying a join,
        is a no-op rather than an IntegrityError -- and crucially does not
        demote an existing owner to member, which a plain upsert of `role`
        would.
        """
        await self._session.execute(
            insert(ProjectMember)
            .values(project_id=project_id, user_id=user_id, role=role)
            .on_conflict_do_nothing(index_elements=["project_id", "user_id"])
        )
