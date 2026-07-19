"""CrispASR TTS engine adapter for the MeowVoice audio-server.

Routes ``/tts`` synthesis to a resident CrispASR OpenAI-compatible TTS server
(C++ ggml, launchd ``dev.nerigate.meowvoice.crispasr``, default port 8123)
instead of the in-process MLX model. Keeping the heavy model in a separate
process is what lets the audio-server honour the MLX/CrispASR mutual-exclusion
invariant (it never loads MLX weights while this engine is active).

Sentence segmentation (bounded)
-------------------------------
CrispASR 1.7B long-sentence RTF is 1.3-1.7 (slower than realtime), so a single
long request stalls first-audio. ``split_sentences`` splits on sentence-ending
punctuation (full-width + ASCII), then bounds every unit to ``max_chars``:
over-long units are sub-split on secondary punctuation and, if still over the
cap, HARD-split by character count. Pure-punctuation / whitespace units are
dropped (never sent to synthesis). Ordering is preserved end to end.

Failure containment (ticket 6-1 R1 / DarkMeow C1)
-------------------------------------------------
A resident CrispASR that goes deaf must not wedge the shared TTS lock. Three
guards bound the blast radius:

* per-sentence request timeout (``timeout``, default 10 s) + at most
  ``retries`` fast retries within a short ``backoff``;
* a whole-utterance ``deadline`` (default 30 s): every request timeout is
  clamped to the remaining budget and the utterance aborts once it is spent;
* a fast circuit breaker: ``breaker_threshold`` consecutive sentence failures
  (or a fully-failed utterance) trips the engine ``unhealthy`` for
  ``breaker_ttl`` seconds. Callers check ``is_unhealthy()`` BEFORE taking the
  TTS lock, so new requests fail-fast (503) instead of queuing behind a dead
  upstream.

Single-sentence failure recovery stays SKIP-AND-CONTINUE (跳句續播): one
isolated failed clause is dropped and the utterance keeps flowing; only
sustained failure trips the breaker or (all clauses failed) raises.
"""

from __future__ import annotations

import asyncio
import io
import time
import wave
from dataclasses import dataclass, field

import httpx

# CrispASR PCM stream contract (matches P-1 measurement protocol):
# response_format=pcm => headerless 24 kHz mono s16le.
CRISPASR_SAMPLE_RATE = 24000

# Primary sentence terminators: full-width + ASCII sentence enders + newline.
_SENTENCE_ENDERS = frozenset("。！？；…!?;.\n")
# Secondary breakpoints used to bound an over-long clause before a hard cut.
_CLAUSE_ENDERS = frozenset("，,、：:")


class CrispasrSynthError(RuntimeError):
    """Utterance could not be synthesized (all clauses failed / deadline / breaker)."""


def _has_content(s: str) -> bool:
    """True if the unit carries at least one letter/digit/CJK char (not pure punctuation)."""
    return any(ch.isalnum() for ch in s)


def _split_on(text: str, enders: frozenset[str]) -> list[str]:
    """Accumulate chars, flushing (terminator included) after each ender; drop blanks."""
    out: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in enders:
            chunk = "".join(buf).strip()
            if chunk:
                out.append(chunk)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _hard_chunk(s: str, max_chars: int) -> list[str]:
    """Last-resort fixed-width cut so no unit ever exceeds ``max_chars``."""
    return [s[i : i + max_chars] for i in range(0, len(s), max_chars)]


def _bound(unit: str, max_chars: int) -> list[str]:
    """Force ``unit`` under ``max_chars``: comma sub-split first, then a hard cut."""
    if len(unit) <= max_chars:
        return [unit]
    out: list[str] = []
    for clause in _split_on(unit, _CLAUSE_ENDERS):
        if len(clause) <= max_chars:
            out.append(clause)
        else:
            out.extend(_hard_chunk(clause, max_chars))
    return out


def split_sentences(text: str, max_chars: int = 60) -> list[str]:
    """Split ``text`` into ordered, length-bounded, content-bearing synthesis units.

    Before: "今天天氣很好。你好嗎？"  ->  After: ["今天天氣很好。", "你好嗎？"]
    A 121-char no-punctuation blob is hard-cut into <=max_chars pieces; a bare
    "……。" yields nothing (pure punctuation is never synthesized).
    """
    units: list[str] = []
    for unit in _split_on(text, _SENTENCE_ENDERS):
        units.extend(_bound(unit, max_chars))
    return [u for u in units if _has_content(u)]


@dataclass
class SynthResult:
    """Ordered concatenation of successfully synthesized clauses + drop bookkeeping."""

    pcm: bytes
    sample_rate: int
    sentences: list[str]
    failed: list[int] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.failed)


class CrispasrTtsEngine:
    """Adapter to a resident CrispASR OpenAI-compatible ``/v1/audio/speech``.

    Owns the synthesis policy (segmentation, retry, skip-and-continue, deadline,
    circuit breaker) but not the HTTP client — the caller passes a shared
    ``httpx.AsyncClient`` so pooling/lifecycle stay with the server process.
    """

    def __init__(
        self,
        url: str,
        voice: str,
        consent: str,
        *,
        sample_rate: int = CRISPASR_SAMPLE_RATE,
        max_chars: int = 60,
        timeout: float = 10.0,
        retries: int = 1,
        backoff: float = 2.0,
        deadline: float = 30.0,
        breaker_threshold: int = 2,
        breaker_ttl: float = 30.0,
    ) -> None:
        self.url = url
        self.voice = voice
        self.consent = consent
        self.sample_rate = sample_rate
        self.max_chars = max_chars
        self.timeout = timeout
        self.retries = retries  # extra attempts after the first, per sentence
        self.backoff = backoff
        self.deadline = deadline  # whole-utterance wall budget (seconds)
        self.breaker_threshold = breaker_threshold
        self.breaker_ttl = breaker_ttl
        # monotonic instant until which the engine is considered unhealthy (0 = ok)
        self._unhealthy_until = 0.0

    # --- circuit breaker ---------------------------------------------------
    def is_unhealthy(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return now < self._unhealthy_until

    def trip(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._unhealthy_until = now + self.breaker_ttl

    def reset(self) -> None:
        self._unhealthy_until = 0.0

    # --- synthesis ---------------------------------------------------------
    async def synth(
        self, client: httpx.AsyncClient, text: str, voice: str | None = None
    ) -> SynthResult:
        """Segment ``text`` and synthesize each clause in order within the deadline.

        Raises ``CrispasrSynthError`` on empty input, deadline exhaustion, a
        tripped breaker, or a fully-failed utterance (so ``/tts`` never returns
        a silent WAV and never hangs past the deadline).
        """
        sentences = split_sentences(text, self.max_chars)
        if not sentences:
            raise CrispasrSynthError("empty text after segmentation")

        deadline_at = time.monotonic() + self.deadline
        chunks: list[bytes] = []
        failed: list[int] = []
        consecutive = 0
        for idx, sentence in enumerate(sentences):
            if time.monotonic() >= deadline_at:
                self.trip()
                raise CrispasrSynthError(f"utterance deadline {self.deadline}s exceeded")
            pcm = await self._synth_one(client, sentence, voice or self.voice, deadline_at)
            if pcm is None:
                failed.append(idx)
                consecutive += 1
                # sustained failure => trip breaker and abort (don't grind all clauses)
                if consecutive >= self.breaker_threshold:
                    self.trip()
                    raise CrispasrSynthError(
                        f"{consecutive} consecutive sentence failures; engine tripped"
                    )
                continue  # isolated failure => skip-and-continue
            consecutive = 0
            chunks.append(pcm)

        if not chunks:
            self.trip()
            raise CrispasrSynthError(f"all {len(sentences)} sentence(s) failed")
        return SynthResult(b"".join(chunks), self.sample_rate, sentences, failed)

    async def _synth_one(
        self, client: httpx.AsyncClient, sentence: str, voice: str, deadline_at: float
    ) -> bytes | None:
        """Synthesize one clause; return PCM or ``None`` after retries/budget.

        A response is a failure if it is non-200 or an empty body (the P-1
        "200 + 0 bytes" trap). Every request timeout is clamped to the time left
        before the utterance deadline so a hung upstream cannot outlast it.
        """
        body = {
            "input": sentence,
            "voice": voice,
            "response_format": "pcm",
            "stream": True,
            "consent_attestation": self.consent,
        }
        for attempt in range(self.retries + 1):
            remaining = deadline_at - time.monotonic()
            if remaining <= 0.1:
                return None
            try:
                resp = await client.post(
                    self.url, json=body, timeout=min(self.timeout, remaining)
                )
                if resp.status_code == 200 and resp.content:
                    return resp.content
            except httpx.HTTPError:
                pass  # transient transport error -> retry / give up
            if attempt < self.retries:
                budget = deadline_at - time.monotonic()
                if self.backoff > 0 and budget > 0:
                    await asyncio.sleep(min(self.backoff, budget))
        return None

    def pcm_to_wav(self, pcm: bytes, sample_rate: int | None = None) -> bytes:
        """Wrap raw s16le mono PCM into a WAV container."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate or self.sample_rate)
            wf.writeframes(pcm)
        return buf.getvalue()
