"""GroqVisionClient's error classification and rate-limit pacing.

No network: every test builds the provider's response by hand. These cover
the decisions that are invisible in the worker-level tests -- which every
inject a fake client and so never exercise this file at all.
"""

import groq
import httpx
import pytest

from backend.services.vision_client import (
    UnusableFramesError,
    VisionError,
    _is_rate_limit,
    _parse_duration,
    _retry_after_seconds,
)


def _status_error(status: int, *, body: dict | None = None, headers: dict | None = None):
    """An APIStatusError shaped like the ones Groq actually returns."""
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(status, request=request, headers=headers or {}, json=body or {})
    return groq.APIStatusError("boom", response=response, body=body)


# --- classification --------------------------------------------------------


def test_a_413_carrying_a_rate_limit_code_is_retryable() -> None:
    """The live incident this exists for. Groq answers a token-per-minute
    overage with **413**, not 429 -- and 413 otherwise means "these images
    are too big", which is permanent. Classified on status alone, a rate
    limit dead-letters the job on a condition that clears in 20 seconds.
    """
    exc = _status_error(
        413,
        body={
            "error": {
                "message": "Request too large ... on tokens per minute (TPM): Limit 8000",
                "type": "tokens",
                "code": "rate_limit_exceeded",
            }
        },
    )

    assert _is_rate_limit(exc) is True


def test_a_plain_413_is_still_permanent() -> None:
    """The other side of that boundary: genuinely oversized images must not
    be retried, because identical bytes fail identically forever."""
    exc = _status_error(413, body={"error": {"message": "payload too large", "type": "invalid_request_error"}})

    assert _is_rate_limit(exc) is False


def test_a_429_is_a_rate_limit_whatever_the_body_says() -> None:
    assert _is_rate_limit(_status_error(429, body={})) is True


def test_a_400_for_bad_frames_is_not_a_rate_limit() -> None:
    exc = _status_error(400, body={"error": {"message": "Too many images provided", "type": "invalid_request_error"}})

    assert _is_rate_limit(exc) is False


# --- how long to wait ------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [("19.44s", 19.44), ("2m59.56s", 179.56), ("14m24s", 864.0), ("1h2m3s", 3723.0)],
)
def test_groqs_duration_formats_are_understood(value, expected) -> None:
    """Verified against the live header, which reads e.g.
    `x-ratelimit-reset-tokens: 19.44s`."""
    assert _parse_duration(value) == pytest.approx(expected)


def test_an_unparseable_duration_is_none_not_zero() -> None:
    """Zero would busy-loop straight back into the same limit."""
    assert _parse_duration("nonsense") is None


def test_the_wait_comes_from_the_providers_own_reset_header() -> None:
    exc = _status_error(413, headers={"x-ratelimit-reset-tokens": "19.44s"})

    # The header plus a moment for the window to actually tick over.
    assert _retry_after_seconds(exc, 0) == pytest.approx(19.94)


def test_retry_after_wins_over_the_reset_header() -> None:
    exc = _status_error(429, headers={"retry-after": "5", "x-ratelimit-reset-tokens": "19.44s"})

    assert _retry_after_seconds(exc, 0) == pytest.approx(5.5)


def test_without_headers_it_backs_off_exponentially() -> None:
    exc = _status_error(429)

    assert _retry_after_seconds(exc, 0) == 1.0
    assert _retry_after_seconds(exc, 3) == 8.0


def test_no_single_wait_can_stall_the_job_indefinitely() -> None:
    """A provider reporting a 14-minute reset must not silently park the
    stage for 14 minutes -- failing with a reason beats hanging."""
    exc = _status_error(429, headers={"x-ratelimit-reset-tokens": "14m24s"})

    assert _retry_after_seconds(exc, 0) == 90.0


# --- the pacing loop itself ------------------------------------------------


class _FakeCompletions:
    """Stands in for groq's client.chat.completions, counting calls."""

    def __init__(self, failures: int, error) -> None:
        self._remaining = failures
        self._error = error
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._error
        return _response_with('{"frames": [{"id": "f0", "visible": true, "confidence": 1.0}]}')


def _response_with(content: str):
    class _Message:
        def __init__(self) -> None:
            self.content = content

    class _Choice:
        def __init__(self) -> None:
            self.message = _Message()

    class _Response:
        def __init__(self) -> None:
            self.choices = [_Choice()]

    return _Response()


def _client_with(completions, **kwargs):
    from backend.services.vision_client import GroqVisionClient

    client = GroqVisionClient(api_key="test", model="test-model", **kwargs)
    client._client.chat.completions = completions  # type: ignore[assignment]
    return client


@pytest.mark.asyncio
async def test_a_rate_limited_batch_is_retried_not_failed(monkeypatch) -> None:
    """The point of pacing. A search legitimately needs more than one
    minute of token budget, and failing the stage would restart from frame
    zero and re-bill every frame already inspected -- find_content is
    deliberately uncached, so nothing survives the restart.
    """
    slept: list[float] = []

    async def no_real_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("backend.services.vision_client.asyncio.sleep", no_real_sleep)
    completions = _FakeCompletions(
        2, _status_error(413, body={"error": {"code": "rate_limit_exceeded"}},
                         headers={"x-ratelimit-reset-tokens": "19.44s"})
    )
    client = _client_with(completions, rate_limit_retries=5)

    matches = await client.inspect_frames([("f0", b"jpeg")], query="a red shirt")

    assert completions.calls == 3, "should have retried twice then succeeded"
    assert [round(s, 2) for s in slept] == [19.94, 19.94], "waits come from the reset header"
    assert matches[0].visible is True


@pytest.mark.asyncio
async def test_a_rate_limit_that_never_clears_fails_retryably(monkeypatch) -> None:
    """Retryable, not permanent: the budget refills eventually, so the
    stage should come back rather than dead-letter."""

    async def no_real_sleep(seconds):
        return None

    monkeypatch.setattr("backend.services.vision_client.asyncio.sleep", no_real_sleep)
    completions = _FakeCompletions(
        99, _status_error(429, body={"error": {"code": "rate_limit_exceeded"}})
    )
    client = _client_with(completions, rate_limit_retries=2)

    with pytest.raises(VisionError):
        await client.inspect_frames([("f0", b"jpeg")], query="x")

    assert completions.calls == 3, "the initial attempt plus two retries"


@pytest.mark.asyncio
async def test_oversized_frames_are_not_retried(monkeypatch) -> None:
    """A permanent rejection must fail on the first call -- retrying
    identical bytes spends the budget for an identical answer."""
    completions = _FakeCompletions(
        99, _status_error(413, body={"error": {"message": "payload too large"}})
    )
    client = _client_with(completions, rate_limit_retries=5)

    with pytest.raises(UnusableFramesError):
        await client.inspect_frames([("f0", b"jpeg")], query="x")

    assert completions.calls == 1, "a permanent rejection must not be retried"


@pytest.mark.asyncio
async def test_invalid_json_from_the_model_is_re_asked_not_dead_lettered(monkeypatch) -> None:
    """json_object mode makes the provider *check* the JSON, not guarantee
    it. Observed live, mid-search: the model emitted

        {"frames": [{"id": "f20", ...}], [{"id": "f21", ...}]}

    -- a malformed second array -- and Groq answered 400
    json_validate_failed. Every 400 used to be permanent, so one unlucky
    generation dead-lettered a search that had already paid for every frame
    inspected before it. Asking again re-rolls the sample.
    """

    async def no_real_sleep(seconds):
        return None

    monkeypatch.setattr("backend.services.vision_client.asyncio.sleep", no_real_sleep)
    completions = _FakeCompletions(
        1,
        _status_error(
            400,
            body={
                "error": {
                    "code": "json_validate_failed",
                    "message": "Failed to generate JSON.",
                    "failed_generation": '{"frames": [{"id": "f0"}], [{"id": "f1"}]}',
                }
            },
        ),
    )
    client = _client_with(completions, rate_limit_retries=3)

    matches = await client.inspect_frames([("f0", b"jpeg")], query="x")

    assert completions.calls == 2, "should have re-asked once and then succeeded"
    assert matches[0].visible is True


@pytest.mark.asyncio
async def test_persistent_invalid_json_eventually_gives_up(monkeypatch) -> None:
    """Bounded, so a model that cannot satisfy the schema at all fails
    rather than looping on the caller's budget."""

    async def no_real_sleep(seconds):
        return None

    monkeypatch.setattr("backend.services.vision_client.asyncio.sleep", no_real_sleep)
    completions = _FakeCompletions(
        99, _status_error(400, body={"error": {"code": "json_validate_failed"}})
    )
    client = _client_with(completions, rate_limit_retries=2)

    with pytest.raises(UnusableFramesError):
        await client.inspect_frames([("f0", b"jpeg")], query="x")

    assert completions.calls == 3, "the initial attempt plus two re-asks"
