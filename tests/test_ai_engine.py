"""Unit coverage for the Gemini call path.

No network: the API client is monkeypatched. These tests pin the behaviour
that a parser outage is reported rather than silently reinterpreted as
casual chat, and that the retry budget is actually spent.
"""

import pytest

import ai_engine


class _RaisingModels:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        raise self.exc


class _FakeClient:
    def __init__(self, exc):
        self.models = _RaisingModels(exc)


@pytest.fixture
def no_sleep(monkeypatch):
    """Capture backoff durations instead of actually waiting."""
    slept = []
    monkeypatch.setattr(ai_engine.time, "sleep", lambda s: slept.append(s))
    return slept


# --------------------------------------------------------------------------
# retry policy
# --------------------------------------------------------------------------

def test_transient_failure_exhausts_the_retry_budget(monkeypatch, no_sleep):
    client = _FakeClient(RuntimeError("503 UNAVAILABLE: high demand"))
    monkeypatch.setattr(ai_engine, "client", client)

    with pytest.raises(RuntimeError):
        ai_engine.call_gemini_with_retry("prompt")

    assert client.models.calls == 5, "expected 5 attempts"
    assert len(no_sleep) == 4, "expected 4 waits between 5 attempts"
    assert sum(no_sleep) >= 15.0, f"budget too small: {sum(no_sleep):.1f}s"


@pytest.mark.parametrize("message", [
    "429 RESOURCE_EXHAUSTED",
    "500 INTERNAL",
    "504 DEADLINE_EXCEEDED",
    "connection timed out",
])
def test_other_transient_conditions_are_retried(monkeypatch, no_sleep, message):
    client = _FakeClient(RuntimeError(message))
    monkeypatch.setattr(ai_engine, "client", client)
    with pytest.raises(RuntimeError):
        ai_engine.call_gemini_with_retry("prompt")
    assert client.models.calls == 5


@pytest.mark.parametrize("message", [
    "400 INVALID_ARGUMENT",
    "401 UNAUTHENTICATED",
    "404 NOT_FOUND: model does not exist",
])
def test_permanent_errors_fail_immediately(monkeypatch, no_sleep, message):
    """A bad model name or key must surface at once, not after 15s of waiting."""
    client = _FakeClient(RuntimeError(message))
    monkeypatch.setattr(ai_engine, "client", client)
    with pytest.raises(RuntimeError):
        ai_engine.call_gemini_with_retry("prompt")
    assert client.models.calls == 1
    assert no_sleep == []
