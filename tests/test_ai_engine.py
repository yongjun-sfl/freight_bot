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


# --------------------------------------------------------------------------
# parse failure signalling
# --------------------------------------------------------------------------

def test_api_failure_reports_parse_failed(monkeypatch):
    monkeypatch.setattr(ai_engine, "client", object())

    def boom(*args, **kwargs):
        raise RuntimeError("503 UNAVAILABLE: high demand")

    monkeypatch.setattr(ai_engine, "call_gemini_with_retry", boom)

    out = ai_engine.parse_text_with_llm("200 to e2f loaded")
    assert out["case_type"] == "PARSE_FAILED"
    assert "503" in out["parse_error"]


def test_missing_api_key_reports_parse_failed(monkeypatch):
    monkeypatch.setattr(ai_engine, "client", None)
    out = ai_engine.parse_text_with_llm("200 to e2f loaded")
    assert out["case_type"] == "PARSE_FAILED"


def test_blank_text_is_not_a_parse_failure():
    """Empty input is genuinely nothing to do, not an outage."""
    assert ai_engine.parse_text_with_llm("")["case_type"] == "NONE_WORK_RELATED"
    assert ai_engine.parse_text_with_llm("   ")["case_type"] == "NONE_WORK_RELATED"


async def test_prepare_text_intent_propagates_parse_failure(monkeypatch):
    monkeypatch.setattr(ai_engine, "client", object())

    def boom(*args, **kwargs):
        raise RuntimeError("503 UNAVAILABLE")

    monkeypatch.setattr(ai_engine, "call_gemini_with_retry", boom)

    out = await ai_engine.prepare_text_intent("200 to e2f")
    assert out["case_type"] == "PARSE_FAILED"
    assert out["raw_text"] == "200 to e2f"


async def test_prepare_text_intent_does_not_block_the_event_loop(monkeypatch):
    """The blocking Gemini call must run in an executor, not inline."""
    import asyncio

    monkeypatch.setattr(ai_engine, "client", object())

    def slow(*args, **kwargs):
        ai_engine.time.sleep(0.4)
        raise RuntimeError("503 UNAVAILABLE")

    monkeypatch.setattr(ai_engine, "call_gemini_with_retry", slow)

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    await ai_engine.prepare_text_intent("200 to e2f")
    beat.cancel()

    # If the call ran inline the loop would be frozen and ticks would be 0.
    assert ticks >= 3, f"event loop stalled during parse (ticks={ticks})"


# --------------------------------------------------------------------------
# vision failure signalling
# --------------------------------------------------------------------------

def test_ocr_failure_on_every_image_is_flagged(monkeypatch):
    monkeypatch.setattr(ai_engine, "client", object())
    monkeypatch.setattr(ai_engine, "compress_image", lambda b, max_dim=1800: b)

    def boom(*args, **kwargs):
        raise RuntimeError("503 UNAVAILABLE")

    monkeypatch.setattr(ai_engine, "call_gemini_with_retry", boom)

    out = ai_engine.extract_bol_locally([b"img-a", b"img-b"])
    assert out["ocr_failed"] is True


def test_successful_scan_with_no_document_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(ai_engine, "client", object())
    monkeypatch.setattr(ai_engine, "compress_image", lambda b, max_dim=1800: b)

    class _Resp:
        text = '{"is_paper_document": false, "bol_number": null, "document_type": "UNKNOWN", "trailer_number": null, "shipper_signed": false, "receiver_signed": false}'

    monkeypatch.setattr(ai_engine, "call_gemini_with_retry", lambda *a, **k: _Resp())

    out = ai_engine.extract_bol_locally([b"a-photo-of-a-truck"])
    assert out["ocr_failed"] is False
    assert out["is_paper_document"] is False
