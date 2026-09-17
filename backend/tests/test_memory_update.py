"""Phase 6 — the memory loop's write half.

Preferences have been read before every planning call since Phase 2;
nothing ever wrote them. These cover the writing.

The LLM call is faked. What it returns is not the interesting part — the
interesting parts are that a *sparse* result is applied without erasing
what it didn't mention, that the endpoint is safe to call repeatedly, and
that a failed extraction never turns a successful job into a failed
request.
"""

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.models import (
    Conversation,
    Job,
    JobStatus,
    Message,
    MessageRole,
    Project,
    UserPreference,
)
from backend.services.llm_client import LLMClient, LLMClientError
from backend.services.memory_extractor import MemoryExtractor, _clean, _render
from backend.services.memory_update_service import (
    CONVERSATION_ID_KEY,
    JobNotFoundError,
    MemoryUpdateService,
)

pytestmark = pytest.mark.usefixtures("database_url")


class FakeLLM(LLMClient):
    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self._result = result if result is not None else {}
        self._error = error
        self.calls = 0

    async def complete(self, prompt, *, response_schema):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result


@pytest.fixture
async def engine(database_url: str):
    eng = create_async_engine(database_url, poolclass=NullPool)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine):
    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def _seed(
    sessionmaker,
    *,
    status: JobStatus = JobStatus.COMPLETED,
    turns: list[tuple[MessageRole, str]] | None = None,
    link_conversation: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A project + conversation + completed job, as confirm-proposal
    would have left them. Returns (job_id, conversation_id, user_id)."""
    project_id, conversation_id = uuid.uuid4(), uuid.uuid4()
    user_id, job_id = uuid.uuid4(), uuid.uuid4()

    async with sessionmaker() as session:
        session.add(Project(id=project_id, owner_id=user_id))
        await session.flush()  # the conversation's FK needs the project to exist
        session.add(Conversation(id=conversation_id, project_id=project_id))
        await session.flush()
        for role, content in turns or [(MessageRole.USER, "always export for linkedin")]:
            session.add(
                Message(
                    conversation_id=conversation_id,
                    role=role,
                    sender_id=user_id if role == MessageRole.USER else None,
                    content=content,
                )
            )
        payload = {CONVERSATION_ID_KEY: str(conversation_id)} if link_conversation else {}
        session.add(
            Job(
                id=job_id,
                status=status,
                workflow={"workflow": ["crop"]},
                current_stage=1,
                payload=payload,
                max_attempts=3,
            )
        )
        await session.commit()
    return job_id, conversation_id, user_id


async def _cleanup(engine, project_owner: uuid.UUID, job_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text("DELETE FROM jobs WHERE id = :j"), {"j": job_id})
        await conn.execute(
            sa.text("DELETE FROM projects WHERE owner_id = :o"), {"o": project_owner}
        )
        await conn.execute(
            sa.text("DELETE FROM user_preferences WHERE user_id = :u"), {"u": project_owner}
        )


# --- extractor internals (pure) --------------------------------------------


def test_nulls_are_dropped_so_an_old_preference_is_not_erased() -> None:
    """The model returns null for "no evidence". Writing that through
    would wipe a preference the user set in an earlier session."""
    assert _clean({"preferred_platform": "linkedin", "preferred_resolution": None}) == {
        "preferred_platform": "linkedin"
    }


def test_values_are_normalised() -> None:
    assert _clean({"preferred_platform": "  LinkedIn  "}) == {"preferred_platform": "linkedin"}


def test_captions_flag_must_be_a_real_boolean() -> None:
    """A string "yes" would be truthy and silently enable captions."""
    assert _clean({"captions_enabled": "yes"}) == {}
    assert _clean({"captions_enabled": False}) == {"captions_enabled": False}


def test_unknown_fields_are_ignored() -> None:
    assert _clean({"favourite_colour": "blue"}) == {}


def test_transcript_labels_who_spoke() -> None:
    """The prompt tells the model to ignore what the assistant proposed,
    which it can only do if the two are distinguishable."""
    rendered = _render(
        [
            Message(conversation_id=uuid.uuid4(), role=MessageRole.USER, content="hi"),
            Message(conversation_id=uuid.uuid4(), role=MessageRole.ASSISTANT, content="hello"),
        ]
    )

    assert rendered == "User: hi\nAssistant: hello"


# --- the service -----------------------------------------------------------


@pytest.mark.asyncio
async def test_extracted_preferences_are_persisted(engine, sessionmaker) -> None:
    job_id, _, user_id = await _seed(sessionmaker)
    llm = FakeLLM({"preferred_platform": "linkedin", "captions_enabled": True})

    try:
        async with sessionmaker() as session:
            result = await MemoryUpdateService(session, MemoryExtractor(llm)).update_from_job(job_id)

        assert result.processed is True
        assert result.updated_fields == ["captions_enabled", "preferred_platform"]
        async with sessionmaker() as session:
            stored = await session.get(UserPreference, user_id)
        assert stored.preferred_platform == "linkedin"
        assert stored.captions_enabled is True
    finally:
        await _cleanup(engine, user_id, job_id)


@pytest.mark.asyncio
async def test_a_second_call_does_no_work_and_costs_no_llm_call(engine, sessionmaker) -> None:
    """The frontend calls this from a poll loop that may double-fire across
    tabs, retries and refreshes."""
    job_id, _, user_id = await _seed(sessionmaker)
    llm = FakeLLM({"preferred_platform": "linkedin"})
    extractor = MemoryExtractor(llm)

    try:
        async with sessionmaker() as session:
            first = await MemoryUpdateService(session, extractor).update_from_job(job_id)
        async with sessionmaker() as session:
            second = await MemoryUpdateService(session, extractor).update_from_job(job_id)

        assert first.processed is True
        assert second.processed is False
        assert llm.calls == 1, "the second call must not pay for another LLM round-trip"
    finally:
        await _cleanup(engine, user_id, job_id)


@pytest.mark.asyncio
async def test_a_partial_result_does_not_erase_other_preferences(engine, sessionmaker) -> None:
    """A conversation that only mentions captions must not wipe the
    platform preference set three sessions ago."""
    job_id, _, user_id = await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(UserPreference(user_id=user_id, preferred_platform="tiktok"))
        await session.commit()

    try:
        async with sessionmaker() as session:
            await MemoryUpdateService(
                session, MemoryExtractor(FakeLLM({"captions_enabled": True}))
            ).update_from_job(job_id)

        async with sessionmaker() as session:
            stored = await session.get(UserPreference, user_id)
        assert stored.captions_enabled is True
        assert stored.preferred_platform == "tiktok", "untouched field was erased"
    finally:
        await _cleanup(engine, user_id, job_id)


@pytest.mark.asyncio
async def test_a_failed_llm_call_is_not_the_users_problem(engine, sessionmaker) -> None:
    """The job succeeded. Failing to learn from it is a soft failure and
    must not surface as a failed request."""
    job_id, _, user_id = await _seed(sessionmaker)

    try:
        async with sessionmaker() as session:
            result = await MemoryUpdateService(
                session, MemoryExtractor(FakeLLM(error=LLMClientError("groq down")))
            ).update_from_job(job_id)

        assert result.processed is True
        assert result.updated_fields == []
    finally:
        await _cleanup(engine, user_id, job_id)


@pytest.mark.asyncio
async def test_an_unfinished_job_teaches_nothing(engine, sessionmaker) -> None:
    """A running or failed edit says nothing durable about preferences."""
    job_id, _, user_id = await _seed(sessionmaker, status=JobStatus.RUNNING)
    llm = FakeLLM({"preferred_platform": "linkedin"})

    try:
        async with sessionmaker() as session:
            result = await MemoryUpdateService(session, MemoryExtractor(llm)).update_from_job(job_id)

        assert result.processed is False
        assert llm.calls == 0
    finally:
        await _cleanup(engine, user_id, job_id)


@pytest.mark.asyncio
async def test_a_job_with_no_conversation_is_a_quiet_no_op(engine, sessionmaker) -> None:
    """Video-analysis jobs and anything submitted straight through the
    jobs API carry no conversation. Not an error."""
    job_id, _, user_id = await _seed(sessionmaker, link_conversation=False)

    try:
        async with sessionmaker() as session:
            result = await MemoryUpdateService(
                session, MemoryExtractor(FakeLLM({"preferred_platform": "linkedin"}))
            ).update_from_job(job_id)

        assert result.processed is False
    finally:
        await _cleanup(engine, user_id, job_id)


@pytest.mark.asyncio
async def test_no_llm_configured_still_succeeds(engine, sessionmaker) -> None:
    """A checkout with no GROQ_API_KEY must still serve the endpoint."""
    job_id, conversation_id, user_id = await _seed(sessionmaker)

    try:
        async with sessionmaker() as session:
            result = await MemoryUpdateService(session, None).update_from_job(job_id)

        assert result.processed is False
        async with sessionmaker() as session:
            conversation = await session.get(Conversation, conversation_id)
        assert conversation.memory_processed_at is not None, (
            "should still mark processed — re-asking would produce the same nothing"
        )
    finally:
        await _cleanup(engine, user_id, job_id)


@pytest.mark.asyncio
async def test_unknown_job_raises(sessionmaker) -> None:
    async with sessionmaker() as session:
        with pytest.raises(JobNotFoundError):
            await MemoryUpdateService(session, None).update_from_job(uuid.uuid4())


# --- the endpoint ----------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_success_and_is_idempotent(client, engine, sessionmaker) -> None:
    job_id, _, user_id = await _seed(sessionmaker)

    try:
        first = client.post(f"/jobs/{job_id}/update-memory")
        second = client.post(f"/jobs/{job_id}/update-memory")

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["processed"] is False
    finally:
        await _cleanup(engine, user_id, job_id)


@pytest.mark.asyncio
async def test_endpoint_404s_for_an_unknown_job(client) -> None:
    assert client.post(f"/jobs/{uuid.uuid4()}/update-memory").status_code == 404


@pytest.mark.asyncio
async def test_get_job_has_no_hidden_side_effect(client, engine, sessionmaker) -> None:
    """The deliberate design choice: polling must not trigger learning."""
    job_id, conversation_id, user_id = await _seed(sessionmaker)

    try:
        client.get(f"/jobs/{job_id}")

        async with sessionmaker() as session:
            conversation = await session.get(Conversation, conversation_id)
        assert conversation.memory_processed_at is None
    finally:
        await _cleanup(engine, user_id, job_id)


@pytest.mark.parametrize("sentinel", ["null", "None", " n/a ", "unknown", "-", ""])
def test_word_shaped_nulls_are_not_stored_as_preferences(sentinel: str) -> None:
    """Observed live: a conversation that never mentioned subtitles came
    back with subtitle_language="null" — the *string*. It stored cleanly
    and would then have been rendered into every future prompt as though
    the user had chosen it. Models express "no value" as a word far more
    often than as JSON null when a field is marked nullable."""
    assert _clean({"subtitle_language": sentinel}) == {}
