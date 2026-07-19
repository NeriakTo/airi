"""Unit tests for crispasr_tts.py (adapter policy).

Covers bounded segmentation (order + hard cap + pure-punctuation drop, R1/H1),
skip-and-continue vs circuit-breaker trip, whole-utterance deadline, breaker
TTL, and PCM/WAV wrapping. CrispASR is replaced by an httpx.MockTransport so the
policy is tested without the resident server; the ASGI /tts + /health surface is
tested in test_server_crispasr.py and the resident path by the P-1 benchmark.
"""

import asyncio
import struct
import wave

import httpx
import pytest

from crispasr_tts import (
    CrispasrSynthError,
    CrispasrTtsEngine,
    split_sentences,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _engine(**kw) -> CrispasrTtsEngine:
    # backoff defaults to 0 in tests so retry paths stay fast (real default is 2s)
    kw.setdefault("backoff", 0.0)
    return CrispasrTtsEngine("http://crispasr/v1/audio/speech", "serena", "CONSENT", **kw)


# --- split_sentences: order + terminators ---

def test_split_on_sentence_enders_keeps_terminator():
    assert split_sentences("今天天氣很好。你好嗎？") == ["今天天氣很好。", "你好嗎？"]


def test_split_mixed_enders_and_newline():
    assert split_sentences("一。二！三？四；五\n六") == ["一。", "二！", "三？", "四；", "五", "六"]


def test_split_ascii_terminators():
    # ASCII . ! ? are terminators too (H1) so latin sentences segment
    assert split_sentences("Hello. World! Ok?") == ["Hello.", "World!", "Ok?"]


def test_split_no_ender_is_single_unit():
    assert split_sentences("沒有標點的一句話") == ["沒有標點的一句話"]


def test_split_empty_or_whitespace_yields_nothing():
    assert split_sentences("") == []
    assert split_sentences("  \n  ") == []


# --- split_sentences: hard cap + pure-punctuation drop (H1) ---

def test_split_hardcaps_no_punctuation_blob():
    # 121 chars, no punctuation: must be hard-cut into <=max_chars units, in order
    text = "字" * 121
    units = split_sentences(text, max_chars=60)
    assert len(units) == 3  # ceil(121/60)
    assert all(len(u) <= 60 for u in units)
    assert "".join(units) == text  # nothing lost, order preserved


def test_split_overlong_comma_clause_then_hardcap():
    # comma sub-split leaves an 80-char clause -> still hard-cut under the cap
    text = "頭" * 40 + "，" + "尾" * 80 + "。"
    units = split_sentences(text, max_chars=60)
    assert all(len(u) <= 60 for u in units)
    assert "".join(units) == text


def test_split_overlong_clause_falls_back_to_commas():
    text = "甲乙丙，丁戊己，庚辛壬癸。"
    assert split_sentences(text, max_chars=5) == ["甲乙丙，", "丁戊己，", "庚辛壬癸。"]


def test_split_short_clause_not_comma_split():
    assert split_sentences("你好，世界。", max_chars=60) == ["你好，世界。"]


def test_split_drops_pure_punctuation_units():
    # consecutive ellipsis / bare terminators must not be sent to synthesis
    assert split_sentences("……。") == []
    assert split_sentences("測試……。！") == ["測試…"]
    assert split_sentences("好。……") == ["好。"]


# --- synthesis policy (mocked transport) ---

def _pcm_for(marker: int) -> bytes:
    return struct.pack("<3h", marker, marker, marker)


def test_order_preserved():
    """Output PCM concatenation order == input sentence order."""
    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        text = json.loads(request.content)["input"]
        order.append(text)
        return httpx.Response(200, content=_pcm_for(int(text[0])))

    engine = _engine()
    result = asyncio.run(engine.synth(_client(handler), "1一。2二。3三。"))

    assert result.sentences == ["1一。", "2二。", "3三。"]
    assert order == ["1一。", "2二。", "3三。"]
    samples = struct.unpack(f"<{len(result.pcm)//2}h", result.pcm)
    assert samples == (1, 1, 1, 2, 2, 2, 3, 3, 3)
    assert result.failed == []
    assert result.degraded is False


def test_skip_and_continue_drops_failed_sentence():
    """One isolated failing clause is skipped; the rest play, in order (no trip)."""
    def handler(request: httpx.Request) -> httpx.Response:
        import json
        text = json.loads(request.content)["input"]
        if text.startswith("2"):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, content=_pcm_for(int(text[0])))

    engine = _engine(retries=1)
    result = asyncio.run(engine.synth(_client(handler), "1一。2二。3三。"))

    assert result.failed == [1]
    assert result.degraded is True
    assert struct.unpack(f"<{len(result.pcm)//2}h", result.pcm) == (1, 1, 1, 3, 3, 3)
    assert engine.is_unhealthy() is False  # a single isolated failure never trips


def test_empty_body_200_counts_as_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    engine = _engine()
    with pytest.raises(CrispasrSynthError):
        asyncio.run(engine.synth(_client(handler), "只有一句。"))


def test_all_sentences_fail_raises_and_trips():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    engine = _engine()
    assert engine.is_unhealthy() is False
    with pytest.raises(CrispasrSynthError):
        asyncio.run(engine.synth(_client(handler), "一。二。"))
    assert engine.is_unhealthy() is True  # sustained failure trips the breaker


def test_empty_input_raises():
    engine = _engine()
    with pytest.raises(CrispasrSynthError):
        asyncio.run(engine.synth(_client(lambda r: httpx.Response(200)), "   "))


def test_retry_then_succeed():
    """First attempt fails, retry succeeds -> clause kept, not dropped."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="transient")
        return httpx.Response(200, content=_pcm_for(7))

    engine = _engine(retries=1)
    result = asyncio.run(engine.synth(_client(handler), "重試一次。"))
    assert calls["n"] == 2
    assert result.failed == []
    assert struct.unpack("<3h", result.pcm) == (7, 7, 7)


# --- failure containment (C1) ---

def test_breaker_trips_on_consecutive_failures():
    """Two consecutive failures abort the utterance and trip the breaker."""
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        return httpx.Response(503, text="down")

    engine = _engine(breaker_threshold=2, retries=1)
    with pytest.raises(CrispasrSynthError):
        asyncio.run(engine.synth(_client(handler), "一。二。三。四。"))
    assert engine.is_unhealthy() is True
    # aborted at 2 consecutive fails (2 sentences x (1+retry)=4 requests), not all 4
    assert seen["n"] == 4


def test_breaker_ttl_expiry():
    engine = _engine(breaker_ttl=30)
    engine.trip(now=100.0)
    assert engine.is_unhealthy(now=129.9) is True
    assert engine.is_unhealthy(now=130.1) is False


def test_deadline_zero_aborts_before_any_upstream_call():
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, content=_pcm_for(1))

    engine = _engine(deadline=0.0)
    with pytest.raises(CrispasrSynthError):
        asyncio.run(engine.synth(_client(handler), "一。二。"))
    assert called["n"] == 0  # deadline already spent, no upstream call issued
    assert engine.is_unhealthy() is True


def test_pcm_to_wav_roundtrip():
    engine = _engine()
    pcm = _pcm_for(42) * 100
    wav = engine.pcm_to_wav(pcm)
    import io
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 24000
        assert wf.getnframes() == len(pcm) // 2
