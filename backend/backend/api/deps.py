"""Shared FastAPI dependencies.

Two things live here, and the second is the point of the module.

`SessionDep` was previously copy-pasted into three route modules. It is
identical in all of them, so it belongs in one place.

`CurrentUserDep` is Phase 8's identity channel -- the `X-User-Id` header,
or a `user_id` query parameter for the requests a browser cannot put
headers on. Until now identity arrived
-- when it arrived at all -- as a field inside the request body:
`CreateProjectRequest.owner_id`, `PostMessageRequest.sender_id`. Five of
the seven /projects endpoints, including confirm-proposal, carried no
caller identity whatsoever.

**Why a header rather than the body.** Not a stylistic preference: FastAPI
resolves Pydantic bodies *after* dependencies run, so a Depends-based
membership guard structurally cannot read a body field. Identity has to
arrive somewhere a dependency can see it, which means a header (following
the Idempotency-Key precedent already in routes/jobs.py) or a token.

**What this is not.** It is not authentication. `X-User-Id` is asserted by
the client and believed; there is no users table, no credential, nothing
signed. Anyone can claim to be anyone. That is a deliberate scope
decision for this phase -- the collaboration logic Phase 8 is actually
about needs identity to be *consistent and checkable*, not *proven* --
but the honest description is "trust the client".

What it does buy: identity now enters the system through exactly one
function. Swapping in real auth later means changing `current_user_id`
and nothing else, rather than editing every route that reads a user id
out of a body.
"""

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.session import get_session

__all__ = [
    "ArtifactAccessDep",
    "CurrentUserDep",
    "ProjectMemberDep",
    "SessionDep",
    "ProjectOwnerDep",
    "as_user_id",
    "current_user_id",
    "require_artifact_access",
    "require_project_member",
    "require_project_owner",
]

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def current_user_id(
    x_user_id: Annotated[
        str | None,
        Header(
            alias="X-User-Id",
            description=(
                "Who is making this request. Asserted by the client and not "
                "verified -- see backend/api/deps.py. Any UUID identifies a "
                "user; there is no registration step."
            ),
        ),
    ] = None,
    user_id: Annotated[
        str | None,
        Query(
            description=(
                "The caller, for requests that cannot set headers -- a "
                "<video src>, a download link, a WebSocket handshake. "
                "Equivalent to X-User-Id, which wins if both are given."
            )
        ),
    ] = None,
) -> uuid.UUID:
    """The caller, as a UUID.

    **Why a query parameter is accepted as well as the header.** A browser
    cannot attach custom headers to the requests that matter most here:
    `<video src="/artifacts?...">` is the entire reason the download route
    streams and honours Range, and `new WebSocket(url)` cannot set them
    either, which Phase 8's membership-checked socket handshake needs.
    Header-only identity would have made both unusable.

    This costs nothing in security *because there is none to lose*: as the
    module docstring says, X-User-Id is asserted by the client and
    believed. A query parameter is exactly as forgeable as a header. It is
    more *visible* -- query strings reach access logs and browser history
    where headers do not -- which would matter for a real credential, and
    is a reason this transport must not be carried over verbatim when
    actual auth replaces it. The header stays the documented default and
    wins when both are present.

    Rejects a malformed value rather than coercing it: a user id that
    isn't a UUID would still be usable as a dict key and would silently
    create a parallel identity that never matches any stored row.
    """
    raw = x_user_id or user_id
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="an X-User-Id header or user_id query parameter is required",
        )
    caller = as_user_id(raw)
    if caller is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="X-User-Id must be a UUID",
        )
    return caller


def as_user_id(raw: str | None) -> uuid.UUID | None:
    """Parse an asserted user id, or None if it is absent or malformed.

    Shared with the room WebSocket, which cannot use `current_user_id`
    directly: that raises HTTPException, and there is no HTTP response to
    put a status on once a handshake is under way -- a socket has to be
    closed with a code instead. Same parsing either way, so the two
    transports can never disagree about what counts as a valid id.
    """
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


CurrentUserDep = Annotated[uuid.UUID, Depends(current_user_id)]


async def require_project_member(
    project_id: uuid.UUID, session: SessionDep, user_id: CurrentUserDep
) -> uuid.UUID:
    """Confirm the caller belongs to this room, and return their id.

    **404, never 403.** A 403 tells a stranger the project exists, which
    is itself information -- whether a given room id is real, and by
    extension whether someone else is working on it. A non-member and a
    non-existent project are indistinguishable from outside, which is the
    correct answer to both.

    A single indexed primary-key lookup, so applying it to every room
    endpoint costs one cheap query rather than a join.
    """
    from backend.repositories.project_member_repository import ProjectMemberRepository

    if not await ProjectMemberRepository(session).is_member(project_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return user_id


ProjectMemberDep = Annotated[uuid.UUID, Depends(require_project_member)]


async def require_project_owner(
    project_id: uuid.UUID, session: SessionDep, user_id: CurrentUserDep
) -> uuid.UUID:
    """Stricter than membership: only the owner may invite.

    A member who could invite could hand the room to anyone, which makes
    the owner's control over who is present meaningless. V1 keeps that
    power in one place; Phase 9a's approval policies are where richer
    roles get designed, and inventing them before then would be guessing.

    404 rather than 403 for a non-member, for the reason in
    require_project_member. A *member* who is not the owner gets 403 --
    they already know the room exists, so there is nothing left to hide,
    and "you are not allowed" is the more useful answer.
    """
    from backend.repositories.project_member_repository import OWNER, ProjectMemberRepository

    role = await ProjectMemberRepository(session).get_role(project_id, user_id)
    if role is None or role not in ("owner", "member"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    if role != OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the project owner can invite members",
        )
    return user_id


ProjectOwnerDep = Annotated[uuid.UUID, Depends(require_project_owner)]


async def require_artifact_access(
    session: SessionDep,
    user_id: CurrentUserDep,
    uri: Annotated[str, Query(description="Opaque storage URI of the artifact to fetch.")],
    job_id: Annotated[
        uuid.UUID,
        Query(
            description=(
                "The job this artifact was listed under -- from a "
                "`download_url` in `GET /jobs/{job_id}/artifacts` or in a "
                "room snapshot's exports. Required: it is the room context "
                "the download is authorized against."
            )
        ),
    ],
) -> str:
    """Confirm the caller may download this artifact, and return its URI.

    A guard rather than an inline check, following the architecture doc's
    own warning about membership checks scattered ad hoc across
    endpoints.

    **404 for every refusal**, matching require_project_member: not a
    member, not that job's artifact, no such job and no such object are
    deliberately indistinguishable. Any other code would confirm that a
    given URI exists, which is the fact worth hiding.
    """
    from backend.services.artifact_access_service import ArtifactAccessService

    if not await ArtifactAccessService(session).is_visible_to(
        uri=uri, job_id=job_id, user_id=user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found"
        )
    return uri


ArtifactAccessDep = Annotated[str, Depends(require_artifact_access)]
