"""ASGI-layer regression tests for the crispasr TTS path (ticket 6-1 R1 / M1).

These drive the real FastAPI routes (/tts, /health) through httpx.ASGITransport
with the module globals monkeypatched to a MockTransport upstream — so the
global TTS lock, the pre-lock breaker fast-fail, the 503 mapping, and the
/health upstream probe are all exercised, not just the adapter in isolation.
"""

import asyncio
import struct
import time

import httpx
import pytest

import server
from crispasr_tts import CrispasrTtsEngine


def _pcm() -> bytes:
    return struct.pack("<3h", 9, 9, 9)


def _mock_http(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _asgi() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://t")


def _install(monkeypatch, upstream_handler, **engine_kw):
    """Point server at crispasr mode with a mocked upstream.

    The module-level asyncio locks are replaced with fresh ones each test: an
    asyncio.Lock binds to the loop that first uses it, so reusing the imported
    lock across separate asyncio.run() loops would raise "bound to a different
    event loop". Fresh locks per test bind to that test's loop.
    """
    engine = CrispasrTtsEngine(server.CRISPASR_URL, "serena", "C", backoff=0.0, **engine_kw)
    monkeypatch.setattr(server, "TTS_ENGINE", "crispasr")
    monkeypatch.setattr(server, "_crispasr", engine)
    monkeypatch.setattr(server, "_http_client", _mock_http(upstream_handler))
    monkeypatch.setattr(server, "_tts_lock", asyncio.Lock())
    monkeypatch.setattr(server, "_crispasr_probe_lock", asyncio.Lock())
    server._crispasr_health.update(ts=0.0, ok=False)  # force a fresh /health probe
    return engine


def test_tts_success_returns_wav(monkeypatch):
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_pcm())

    _install(monkeypatch, upstream)

    async def run():
        async with _asgi() as c:
            return await c.post("/tts", json={"text": "你好。世界。"})

    r = asyncio.run(run())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    assert len(r.content) > 44  # WAV header + samples


def test_tts_upstream_failure_returns_503(monkeypatch):
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    _install(monkeypatch, upstream, breaker_threshold=2)

    async def run():
        async with _asgi() as c:
            return await c.post("/tts", json={"text": "一。二。"})

    r = asyncio.run(run())
    assert r.status_code == 503  # not a silent 200/500


def test_tts_fast_fails_without_blocking_on_lock(monkeypatch):
    """Breaker open => 503 returned even while another caller holds _tts_lock.

    If the endpoint took the lock before checking the breaker this would deadlock
    (same loop already holds it) and wait_for would time out.
    """
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_pcm())

    engine = _install(monkeypatch, upstream)
    engine.trip()  # breaker open

    async def run():
        async with server._tts_lock:  # hold the lock the whole time
            async with _asgi() as c:
                return await asyncio.wait_for(
                    c.post("/tts", json={"text": "你好。"}), timeout=5.0
                )

    r = asyncio.run(run())
    assert r.status_code == 503


def test_health_degraded_when_upstream_down(monkeypatch):
    def upstream(request: httpx.Request) -> httpx.Response:
        # health probe hits GET /health on the upstream base -> report it down
        return httpx.Response(503, text="down")

    _install(monkeypatch, upstream)

    async def run():
        async with _asgi() as c:
            return await c.get("/health")

    r = asyncio.run(run())
    body = r.json()
    assert body["tts_engine"] == "crispasr"
    assert body["status"] == "degraded"
    assert body["crispasr_ready"] is False
    assert body["tts_loaded"] is False  # mutual-exclusion witness
    assert body["tts_sample_rate"] == 24000


def test_health_ok_when_upstream_alive(monkeypatch):
    def upstream(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, content=_pcm())

    _install(monkeypatch, upstream)

    async def run():
        async with _asgi() as c:
            return await c.get("/health")

    r = asyncio.run(run())
    body = r.json()
    assert body["status"] == "ok"
    assert body["crispasr_ready"] is True
    assert body["tts_sample_rate"] == 24000


def test_tts_breaker_trips_while_queued_returns_503(monkeypatch):
    """R3 C1: a request that queued before the breaker tripped must not still
    reach the upstream after acquiring _tts_lock — the double-check under the
    lock returns 503 with zero upstream calls.
    """
    calls = {"n": 0}

    def upstream(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=_pcm())

    engine = _install(monkeypatch, upstream)  # breaker healthy at request entry

    async def run():
        await server._tts_lock.acquire()
        # request enters, passes the pre-lock breaker check (healthy), then blocks
        # on _tts_lock which we hold
        async with _asgi() as c:
            task = asyncio.create_task(c.post("/tts", json={"text": "你好。"}))
            await asyncio.sleep(0.05)  # let it reach the lock await
            engine.trip()             # breaker trips while the request is queued
            server._tts_lock.release()
            return await asyncio.wait_for(task, timeout=5.0)

    r = asyncio.run(run())
    assert r.status_code == 503
    assert calls["n"] == 0  # queued request never hit the dead upstream


def test_health_probe_single_flight_latest_wins(monkeypatch):
    """R3 H2: concurrent cold probes are single-flight — one upstream call, and
    the cache ends on the newest result (a seeded stale 'true' is overwritten).
    """
    calls = {"n": 0}

    def upstream(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="down")  # upstream is actually down now

    _install(monkeypatch, upstream)
    server._crispasr_health.update(ts=0.0, ok=True)  # stale 'true' seeded

    async def run():
        return await asyncio.gather(*[server._probe_crispasr() for _ in range(10)])

    results = asyncio.run(run())
    assert all(r is False for r in results)          # every waiter sees newest result
    assert server._crispasr_health["ok"] is False    # cache not left stale-true
    assert calls["n"] == 1                            # single-flight: exactly one probe


def test_health_probe_monotonic_guard_returns_cached_on_reject(monkeypatch):
    """R4 H2: when the monotonic guard rejects this probe's write (a newer result
    is already cached), the caller returns the CACHED value, not its own older one.
    """
    calls = {"n": 0}

    def upstream(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"status": "ok"})  # this probe would compute ok=True

    _install(monkeypatch, upstream)
    # Seed a NEWER result: a future ts (so it is not treated as fresh and the probe
    # still runs) with ok=False — the latest known truth is "down".
    future_ts = time.monotonic() + 1_000_000.0
    server._crispasr_health.update(ts=future_ts, ok=False)

    async def run():
        return await server._probe_crispasr()

    result = asyncio.run(run())
    assert result is False                              # returned the cached (newer) result
    assert server._crispasr_health["ok"] is False       # older True write was rejected
    assert server._crispasr_health["ts"] == future_ts   # newer ts preserved
    assert calls["n"] == 1                              # the probe did run (reached the guard)
