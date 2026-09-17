"""Project creation, the project-scoped chat loop, and video upload.

Messages and videos are both routed under /projects, not as flat top-level
resources: Conversation and Video both belong to Project (see
backend/models/conversation.py, backend/models/video.py) -- Project is the
aggregate root, and everything hangs off it here for that reason. No
endpoint gets moved or renamed once Phase 8 adds multi-member rooms and
Phase 9 adds proposal approval.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse, StreamingResponse

from backend.api.deps import (
    CurrentUserDep,
    ProjectMemberDep,
    ProjectOwnerDep,
    SessionDep,
)
from backend.api.schemas.artifact import ArtifactResponse
from backend.api.schemas.conversation import (
    ConfirmProposalResponse,
    ConversationHistoryResponse,
    MessageResponse,
    PostMessageRequest,
    PostMessageResponse,
)
from backend.api.schemas.job import JobResponse
from backend.api.schemas.member import (
    AddMemberRequest,
    MemberListResponse,
    MemberResponse,
)
from backend.api.schemas.project import (
    CreateProjectRequest,
    ProjectResponse,
    UpdateProjectRequest,
)
from backend.api.schemas.room import ExportResponse, RoomSnapshotResponse
from backend.api.schemas.video import (
    RenameVideoRequest,
    VideoListResponse,
    VideoSummaryResponse,
    VideoUploadResponse,
)
from backend.models import Project
from backend.repositories.project_member_repository import (
    INVITED,
    OWNER,
    ProjectMemberRepository,
)
from backend.repositories.project_repository import (
    ProjectNotFoundError,
    ProjectRepository,
)
from backend.repositories.video_repository import (
    DuplicateVideoNameError,
    VideoRepository,
)
from backend.services.conversation_service import ConversationService
from backend.services.media_streaming import stream_storage_object
from backend.services.planner_factory import get_default_planner
from backend.services.proposal_confirmation_service import (
    NoPendingProposalError,
    ProposalConfirmationService,
    UnknownVideoHandleError,
)
from backend.services.room_events import get_room_events
from backend.services.room_snapshot_service import RoomSnapshotService
from backend.services.video_rename_service import VideoNotFoundError, VideoRenameService
from backend.services.video_upload_service import VideoUploadService

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    request: CreateProjectRequest, session: SessionDep, user_id: CurrentUserDep
) -> ProjectResponse:
    project = Project(owner_id=user_id, name=request.name)
    ProjectRepository(session).add(project)
    await session.flush()  # assigns project.id before the membership row
    # In the same transaction as the project itself: a project that exists
    # but has no members would be unreachable through the membership
    # guard, including by the person who just created it.
    await ProjectMemberRepository(session).add(project.id, user_id, role=OWNER)
    await session.commit()
    return ProjectResponse(
        id=project.id,
        owner_id=project.owner_id,
        name=project.name,
        approval_policy=project.approval_policy,
        created_at=project.created_at,
    )


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: uuid.UUID,
    request: UpdateProjectRequest,
    session: SessionDep,
    owner_id: ProjectOwnerDep,
) -> ProjectResponse:
    """Change room-level settings. Owner only.

    The only setting today is the approval policy (Phase 9a). There was
    previously no way to reach `admin` at all: `team` is the migration
    default and nothing wrote to the column afterward, so every room was
    permanently unanimous whether that was wanted or not.

    Takes effect immediately, for whatever votes happen from here on --
    a proposal already sitting with partial votes is not "grandfathered"
    into the policy that was active when it was created. That is the
    simpler rule, and it matches what an owner changing the setting
    mid-discussion would actually expect: the new rule applies to what
    happens next.

    ProjectOwnerDep has already resolved and authorized the caller (404
    for a non-member matching require_project_member's reasoning, 403 for
    a member who is not the owner), so the project is guaranteed to exist
    by the time this body runs.
    """
    project = await ProjectRepository(session).get(project_id)
    if request.approval_policy is not None:
        project.approval_policy = request.approval_policy
        await session.commit()
    return ProjectResponse(
        id=project.id,
        owner_id=project.owner_id,
        name=project.name,
        approval_policy=project.approval_policy,
        created_at=project.created_at,
    )


@router.get("/{project_id}", response_model=RoomSnapshotResponse)
async def get_room(
    project_id: uuid.UUID, session: SessionDep, user_id: ProjectMemberDep
) -> RoomSnapshotResponse:
    """Everything needed to render the room, in one request.

    Five separate GETs (project, members, videos, messages, jobs) is a
    slow and racy way to open a room -- the client would stitch together
    five points in time. It is also the socket's reconnect path: refetch
    this, then resume the stream.

    No 404 branch of its own: ProjectMemberDep already 404s for both a
    non-member and a missing project (deliberately indistinguishable, see
    backend/api/deps.py), so by the time this body runs the project
    exists and the caller belongs to it.
    """
    snapshot = await RoomSnapshotService(session).get(project_id)
    return RoomSnapshotResponse(
        # Read here rather than inside RoomSnapshotService: the counter is
        # in-memory bus state, not database state, and joining the two is
        # a transport concern. It is the *same* counter the socket
        # advances, which is the whole point -- a reconnecting client
        # compares two numbers from one source and discards anything it
        # has already seen.
        seq=get_room_events().current_seq(project_id),
        project=ProjectResponse(
            id=snapshot.project.id,
            owner_id=snapshot.project.owner_id,
            name=snapshot.project.name,
            approval_policy=snapshot.project.approval_policy,
            created_at=snapshot.project.created_at,
        ),
        members=[
            MemberResponse(user_id=m.user_id, role=m.role, joined_at=m.joined_at)
            for m in snapshot.members
        ],
        videos=[
            VideoSummaryResponse(
                id=v.id,
                original_filename=v.original_filename,
                name=v.name,
                created_at=v.created_at,
            )
            for v in snapshot.videos
        ],
        messages=[
            MessageResponse(
                id=m.id,
                role=m.role,
                sender_id=m.sender_id,
                content=m.content,
                created_at=m.created_at,
            )
            for m in snapshot.messages
        ],
        active_jobs=[JobResponse.from_model(job) for job in snapshot.active_jobs],
        ended_jobs=[JobResponse.from_model(job) for job in snapshot.ended_jobs],
        exports=[
            ExportResponse(
                job_id=export.job.id,
                workflow=export.job.workflow["workflow"],
                completed_at=export.job.completed_at,
                artifacts=[
                    ArtifactResponse.from_asset(
                        kind=asset.kind, uri=asset.uri, job_id=export.job.id
                    )
                    for asset in export.artifacts
                ],
            )
            for export in snapshot.exports
        ],
    )


@router.post("/{project_id}/messages", response_model=PostMessageResponse)
async def post_message(
    project_id: uuid.UUID,
    request: PostMessageRequest,
    session: SessionDep,
    user_id: ProjectMemberDep,
) -> PostMessageResponse:
    try:
        result = await ConversationService(
            session, planner=get_default_planner()
        ).post_message(
            project_id=project_id, sender_id=user_id, content=request.content
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        ) from exc
    return PostMessageResponse(message_id=result.message_id, response=result.response)


@router.get("/{project_id}/messages", response_model=ConversationHistoryResponse)
async def get_messages(
    project_id: uuid.UUID, session: SessionDep, user_id: ProjectMemberDep
) -> ConversationHistoryResponse:
    try:
        messages = await ConversationService(session).get_history(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        ) from exc
    return ConversationHistoryResponse(
        messages=[
            MessageResponse(
                id=m.id,
                role=m.role,
                sender_id=m.sender_id,
                content=m.content,
                created_at=m.created_at,
            )
            for m in messages
        ]
    )


@router.post(
    "/{project_id}/videos", response_model=VideoUploadResponse, status_code=status.HTTP_201_CREATED
)
async def upload_video(
    project_id: uuid.UUID,
    session: SessionDep,
    user_id: ProjectMemberDep,
    file: Annotated[UploadFile, File()],
    name: Annotated[str | None, Form()] = None,
) -> VideoUploadResponse:
    data = await file.read()
    try:
        result = await VideoUploadService(session).upload(
            project_id=project_id,
            data=data,
            filename=file.filename or "upload",
            name=name,
            uploaded_by=user_id,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        ) from exc
    except DuplicateVideoNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a video named {name!r} already exists in this project",
        ) from exc
    # After the commit (VideoUploadService.upload() commits internally),
    # and only the uploader's own room previously saw this: everyone else
    # only found out about a new video on their next full snapshot
    # refetch, since nothing announced it live.
    get_room_events().publish(
        project_id,
        type="video.created",
        data={
            "id": str(result.video.id),
            "original_filename": result.video.original_filename,
            "name": result.video.name,
            "created_at": result.video.created_at.isoformat(),
        },
    )
    return VideoUploadResponse(
        video_id=result.video.id,
        project_id=project_id,
        job_id=result.job.id,
        name=result.video.name,
        status="analyzing",
    )


# POST /projects/{id}/confirm-proposal was removed in Phase 9a. It
# submitted a real render on one member's say-so, which is precisely the
# decision the room's approval policy now governs -- leaving it in place
# would have made approval opt-in, and any member could have spent the
# room's compute by calling the older endpoint instead. Its replacement
# is POST /proposals/{id}/approve. Previewing stays unapproved and free.


@router.post("/{project_id}/preview-proposal", response_model=ConfirmProposalResponse)
async def preview_proposal(
    project_id: uuid.UUID, session: SessionDep, user_id: ProjectMemberDep
) -> ConfirmProposalResponse:
    """Render the latest proposal fast and low-resolution, for review.

    Deliberately a sibling of confirm-proposal rather than a flag on it:
    previewing is a different intent from committing, and Phase 9a will
    gate the two differently -- a preview is cheap and reversible, a
    confirm is what the room's approval policy governs.

    Returns the same shape as confirm-proposal: a preview is an ordinary
    Job, polled and downloaded through exactly the same endpoints.
    """
    try:
        result = await ProposalConfirmationService(session).preview(
            project_id, user_id=user_id
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        ) from exc
    except NoPendingProposalError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="no pending proposal to preview"
        ) from exc
    except UnknownVideoHandleError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"proposal references unknown video handle '{exc.video_id}'",
        ) from exc
    return ConfirmProposalResponse(job_id=result.job.id, replayed=result.replayed)


@router.get("/{project_id}/videos", response_model=VideoListResponse)
async def list_videos(
    project_id: uuid.UUID, session: SessionDep, user_id: ProjectMemberDep
) -> VideoListResponse:
    if await ProjectRepository(session).get(project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    videos = await VideoRepository(session).list_by_project(project_id)
    return VideoListResponse(
        videos=[
            VideoSummaryResponse(
                id=v.id,
                original_filename=v.original_filename,
                name=v.name,
                created_at=v.created_at,
            )
            for v in videos
        ]
    )


@router.patch("/{project_id}/videos/{video_id}", response_model=VideoSummaryResponse)
async def rename_video(
    project_id: uuid.UUID,
    video_id: uuid.UUID,
    request: RenameVideoRequest,
    session: SessionDep,
    user_id: ProjectMemberDep,
) -> VideoSummaryResponse:
    """Any member may rename any video in the room -- not owner-gated, per
    the same collaborative-by-default posture the rest of the room takes
    (uploading, messaging). Renaming isn't a room-level setting like
    approval policy, just a label on one piece of shared footage.
    """
    try:
        video = await VideoRenameService(session).rename(
            project_id=project_id, video_id=video_id, name=request.name
        )
    except VideoNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="video not found"
        ) from exc
    except DuplicateVideoNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a video named {request.name!r} already exists in this project",
        ) from exc
    get_room_events().publish(
        project_id,
        type="video.updated",
        data={
            "id": str(video.id),
            "original_filename": video.original_filename,
            "name": video.name,
            "created_at": video.created_at.isoformat(),
        },
    )
    return VideoSummaryResponse(
        id=video.id,
        original_filename=video.original_filename,
        name=video.name,
        created_at=video.created_at,
    )


@router.get("/{project_id}/videos/{video_id}/download", response_model=None)
async def download_video(
    project_id: uuid.UUID,
    video_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    user_id: ProjectMemberDep,
) -> StreamingResponse | RedirectResponse:
    """Stream the original uploaded video, before any edit ever touches it.

    A real, previously-unfilled gap: every other watchable thing in this
    API is a job's *output* (GET /artifacts, authorized via
    require_artifact_access against a job that produced the asset) -- but
    a fresh upload's own video_analysis job produces no assets at all
    (it predates the asset model), so there was no sanctioned way to watch
    or download what you just uploaded until an edit had run on it.

    Authorized by room membership alone, same as rename_video above --
    any member may view any video already visible in the room's own
    library (GET /projects/{id}/videos), so this adds no new exposure.
    Deliberately a separate route rather than routing storage_uri through
    GET /artifacts: that route's authorization is job-shaped (a URI plus
    the job that produced it), and a raw upload has no such job.
    """
    video = await VideoRepository(session).get(video_id)
    if video is None or video.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="video not found")
    uri, filename = video.storage_uri, video.original_filename
    # Release the pooled connection before streaming -- see the same call
    # in backend/api/routes/artifacts.py for why holding it across a video
    # download exhausts the pool and takes the whole API down with it.
    # Read what we need off `video` first: the instance is expired once
    # its session closes, so touching it afterwards would re-query.
    await session.close()
    return await stream_storage_object(request, uri, fallback_filename=filename)


@router.post(
    "/{project_id}/members",
    response_model=MemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    project_id: uuid.UUID,
    request: AddMemberRequest,
    session: SessionDep,
    owner_id: ProjectOwnerDep,
) -> MemberResponse:
    """Invite someone into the room. Owner only.

    Creates an *invitation*, not a membership: the invitee has to call
    join before they can see anything. Being added to a room without
    having asked is not the same as being in it, and an invitation that
    granted access immediately would make join a no-op.
    """
    members = ProjectMemberRepository(session)
    await members.add(project_id, request.user_id, role=INVITED)
    await session.commit()

    role = await members.get_role(project_id, request.user_id)
    member = next(
        m for m in await members.list_by_project(project_id) if m.user_id == request.user_id
    )
    return MemberResponse(user_id=member.user_id, role=role, joined_at=member.joined_at)


@router.post("/{project_id}/join", response_model=MemberResponse)
async def join_project(
    project_id: uuid.UUID, session: SessionDep, user_id: CurrentUserDep
) -> MemberResponse:
    """Accept an invitation.

    Deliberately NOT behind the membership guard -- an invitee is not yet
    a member, so the guard would make accepting impossible. It is instead
    gated on holding an invitation, which is what stops this becoming a
    way to walk into any room whose id you happen to know.
    """
    members = ProjectMemberRepository(session)
    if not await members.accept_invitation(project_id, user_id):
        # Covers all three of: no invitation, no such project, and already
        # a member. They are deliberately indistinguishable -- telling a
        # stranger "that room exists but you weren't invited" is the same
        # leak the 404 in require_project_member avoids.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no pending invitation for this project",
        )
    await session.commit()

    member = next(
        m for m in await members.list_by_project(project_id) if m.user_id == user_id
    )
    # After the commit, and only on a real transition: accept_invitation
    # returning False above already sent the no-op case away, so nobody
    # gets announced twice for refreshing the join link.
    get_room_events().publish(
        project_id,
        type="member.joined",
        data={
            "user_id": str(member.user_id),
            "role": member.role,
            "joined_at": member.joined_at.isoformat(),
        },
    )
    return MemberResponse(user_id=member.user_id, role=member.role, joined_at=member.joined_at)


@router.get("/{project_id}/members", response_model=MemberListResponse)
async def list_members(
    project_id: uuid.UUID, session: SessionDep, user_id: ProjectMemberDep
) -> MemberListResponse:
    """Everyone in the room, including outstanding invitations — a member
    should be able to see who has been asked, not just who has arrived."""
    members = await ProjectMemberRepository(session).list_by_project(project_id)
    return MemberListResponse(
        members=[
            MemberResponse(user_id=m.user_id, role=m.role, joined_at=m.joined_at)
            for m in members
        ]
    )
