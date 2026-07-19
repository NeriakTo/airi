"""server.py 預合成快取接線測試（票 6-3）——ASGI 層驅動真實路由。

驗接線：reply-callback 入匣成功觸發預合成 enqueue；GET /voice/reply/{id}/audio
命中快取回檔、miss 即時合成並落快取、reply 不存在回 404。合成以 monkeypatch
的 fake 取代（不打真引擎；真引擎延遲另由 scripts/measure_reply_cache.py 量測）。
"""

import asyncio
import threading
import time

import httpx
import pytest

import server
from reply_box import ReplyBox
from reply_cache import ReplyCache


def _asgi() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://t")


def _install(monkeypatch, tmp_path):
    box = ReplyBox(tmp_path / "box.db")
    cache = ReplyCache(tmp_path / "cache")
    monkeypatch.setattr(server, "_reply_box", box)
    monkeypatch.setattr(server, "_reply_cache", cache)
    monkeypatch.setattr(server, "_check_pin", lambda request: True)
    monkeypatch.setattr(server, "DISCORD_WEBHOOK", "")
    return box, cache


def test_reply_callback_triggers_precache_enqueue(monkeypatch, tmp_path):
    box, _ = _install(monkeypatch, tmp_path)

    enqueued: list[tuple[str, str]] = []

    class SpyCache:
        async def enqueue(self, reply_id, text):
            enqueued.append((reply_id, text))
            return "queued"

        def delete(self, reply_id):
            pass

    monkeypatch.setattr(server, "_reply_cache", SpyCache())

    async def run():
        async with _asgi() as c:
            return await c.post("/voice/reply-callback", json={"text": "回覆內容", "message_id": "m1"})

    r = asyncio.run(run())
    assert r.json()["ok"] is True
    assert enqueued == [("m1", "回覆內容")]  # 入匣成功即觸發預合成


def test_audio_cache_hit_returns_file(monkeypatch, tmp_path):
    box, cache = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "回覆")  # G1：命中前 reply 須在匣內（端點先驗匣再回快取）
    cache.put("m1", b"CACHED-WAV-BYTES")

    async def run():
        async with _asgi() as c:
            return await c.get("/voice/reply/m1/audio")

    r = asyncio.run(run())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    assert r.content == b"CACHED-WAV-BYTES"  # 命中直接回快取檔


def test_audio_cache_miss_synthesizes_and_caches(monkeypatch, tmp_path):
    box, cache = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "現場合成這段")  # 真實時鐘入匣，端點 get 用真實時鐘不判過期
    monkeypatch.setattr(server, "_crispasr", object())  # 非 None：不走 MLX fallback

    async def fake_synth(text: str) -> bytes:
        return b"FRESH:" + text.encode("utf-8")

    async def main():
        cache.start(fake_synth)  # H1：合成一律走 worker，端點不直呼引擎
        async with _asgi() as c:
            r = await c.get("/voice/reply/m1/audio")
        await cache.stop()
        return r

    r = asyncio.run(main())
    assert r.status_code == 200
    assert r.content == "FRESH:現場合成這段".encode("utf-8")
    assert cache.path_if_ready("m1") is not None  # miss 後落快取供下次命中


def test_audio_missing_reply_returns_404(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_crispasr", object())

    async def run():
        async with _asgi() as c:
            return await c.get("/voice/reply/ghost/audio")

    r = asyncio.run(run())
    assert r.status_code == 404


def test_audio_rejects_deleted_id_even_if_cache_present(monkeypatch, tmp_path):
    # G1：快取檔存在但 reply 已不在匣（被刪／到期），端點先驗匣→404，不回復活快取。
    _, cache = _install(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_crispasr", object())
    cache.put("gone", b"STALE-WAV")  # 快取存在，但匣內無 "gone"

    async def run():
        async with _asgi() as c:
            return await c.get("/voice/reply/gone/audio")

    r = asyncio.run(run())
    assert r.status_code == 404


def test_audio_miss_joins_inflight_single_synthesis(monkeypatch, tmp_path):
    # G3：點播 miss 撞進行中的預合成，併入共用結果、不重複合成（引擎呼叫恰一次）。
    box, cache = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "共用這段")  # 真實時鐘入匣，端點 get 不判過期
    monkeypatch.setattr(server, "_crispasr", object())

    calls: list[str] = []
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow_synth(text: str) -> bytes:
        calls.append(text)
        started.set()
        await gate.wait()
        return b"WAV:" + text.encode("utf-8")

    async def main():
        cache.start(slow_synth)
        await cache.enqueue("m1", "共用這段")
        await started.wait()  # worker 取出、進合成中（J4 精確同步）

        async def hit():
            async with _asgi() as c:
                return await c.get("/voice/reply/m1/audio")

        task = asyncio.create_task(hit())
        # NOTICE: 端點併入進行中的 job 是在其內部 await Future，無外部 event 可掛；
        # 保留短 sleep 讓端點協程推進到等待點（J4 改不動處，留註解說明）。
        await asyncio.sleep(0.05)
        gate.set()  # worker 完成合成
        r = await task
        await cache.stop()
        return r, calls

    r, calls = asyncio.run(main())
    assert r.status_code == 200
    assert r.content == "WAV:共用這段".encode("utf-8")
    assert calls == ["共用這段"]  # 只合成一次


def test_lifespan_mlx_sweeps_orphans_and_closes_client(monkeypatch, tmp_path):
    # G4：MLX 回退重啟也清孤兒（sweep_orphans 已移出 crispasr 分支）。
    # G2：shutdown 鏈完整——合成 worker 未 start，stop 為 no-op，aclose 必達。
    box, cache = _install(monkeypatch, tmp_path)
    cache.put("orphan", b"WAV")  # 不在匣
    box.enqueue("keep", "keep")  # 在匣（真實時鐘）
    cache.put("keep", b"WAV")
    monkeypatch.setattr(server, "TTS_ENGINE", "mlx")
    monkeypatch.setattr(server, "_init_pin_storage", lambda: None)
    monkeypatch.setattr(server, "load_tts", lambda: None)
    monkeypatch.setattr(server, "load_stt", lambda: None)
    monkeypatch.setattr(server, "_generate_cached_voices", lambda: None)
    monkeypatch.setattr(server, "_http_client", None)  # 讓 teardown 還原

    async def main():
        async with server.lifespan(server.app):
            pass
        return server._http_client

    client = asyncio.run(main())
    assert cache.path_if_ready("orphan") is None  # MLX 模式也清孤兒
    assert cache.path_if_ready("keep") is not None
    assert client.is_closed  # aclose 必達


# --- 主控裁決 H5：統一合成入口的競態回歸 ---


def test_audio_playback_delete_during_synth_returns_410_no_revival(monkeypatch, tmp_path):
    # H5①：點播觸發合成，合成中該 reply 被刪→端點回 410 且快取不復活（發布守衛）。
    box, cache = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "text")
    monkeypatch.setattr(server, "_crispasr", object())
    gate = asyncio.Event()
    started = asyncio.Event()

    async def stuck_synth(text: str) -> bytes:
        started.set()
        await gate.wait()
        return b"WAV"

    async def main():
        cache.start(stuck_synth)

        async def hit():
            async with _asgi() as c:
                return await c.get("/voice/reply/m1/audio")

        task = asyncio.create_task(hit())
        await started.wait()  # worker 已進合成（端點已建 job、正等待其 Future，J4）
        cache.delete("m1")  # 合成中刪除 → cancel Future + tombstone
        gate.set()  # worker 醒來，發布守衛擋下
        r = await task
        await cache.stop()
        return r

    r = asyncio.run(main())
    assert r.status_code == 410
    assert cache.path_if_ready("m1") is None  # 不復活


def test_audio_timeout_returns_503_no_second_synthesis(monkeypatch, tmp_path):
    # H5②：await 合成逾時→503＋Retry-After，絕不開第二次合成（引擎呼叫恰一次）。
    box, cache = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "text")
    monkeypatch.setattr(server, "_crispasr", object())
    monkeypatch.setattr(server, "REPLY_AUDIO_BUDGET", 0.1)  # 極小預算逼逾時
    calls: list[str] = []
    gate = asyncio.Event()

    async def stuck_synth(text: str) -> bytes:
        calls.append(text)
        await gate.wait()
        return b"WAV"

    async def main():
        cache.start(stuck_synth)
        async with _asgi() as c:
            r = await c.get("/voice/reply/m1/audio")
        gate.set()  # 放行 worker 收束
        await cache.stop()
        return r

    r = asyncio.run(main())
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "1"
    assert calls == ["text"]  # 只有背景那一次，端點逾時未開第二次


def test_audio_joined_future_cancel_returns_410(monkeypatch, tmp_path):
    # H5③：背景預合成進行中、點播 join 同一 Future；reply 被刪→Future 取消→端點
    # 回 410（辨識 fut.cancelled()），不讓 CancelledError 洩漏成 500。
    box, cache = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "text")
    monkeypatch.setattr(server, "_crispasr", object())
    gate = asyncio.Event()
    started = asyncio.Event()

    async def stuck_synth(text: str) -> bytes:
        started.set()
        await gate.wait()
        return b"WAV"

    async def main():
        cache.start(stuck_synth)
        await cache.enqueue("m1", "text")  # 背景預合成 job（inflight）
        await started.wait()  # worker 取出、進合成中（J4 精確同步）

        async def hit():
            async with _asgi() as c:
                return await c.get("/voice/reply/m1/audio")  # 點播 join 同一 Future

        task = asyncio.create_task(hit())
        # NOTICE: 端點 join 在其內部 await Future，無外部 event 可掛；保留短 sleep
        # 讓端點協程推進到等待點（J4 改不動處，留註解說明）。
        await asyncio.sleep(0.05)
        cache.delete("m1")  # 刪除 → cancel joined Future
        gate.set()
        r = await task
        await cache.stop()
        return r

    r = asyncio.run(main())
    assert r.status_code == 410


def test_lifespan_crispasr_shutdown_during_synthesis_is_clean(monkeypatch, tmp_path):
    # H5④：CrispASR 分支 lifespan，合成中 shutdown→worker 收束不炸、aclose 必達。
    # 把黑喵手驗「合成中 shutdown」固定成測試。
    box, cache = _install(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "TTS_ENGINE", "crispasr")
    monkeypatch.setattr(server, "_init_pin_storage", lambda: None)
    monkeypatch.setattr(server, "load_stt", lambda: None)
    monkeypatch.setattr(server, "_crispasr", None)  # 讓 teardown 還原
    monkeypatch.setattr(server, "_http_client", None)
    gate = asyncio.Event()  # 永不 set
    started = asyncio.Event()

    async def noop_precache():
        return None

    async def stuck_synth(text: str) -> bytes:
        started.set()
        await gate.wait()
        return b"never"

    monkeypatch.setattr(server, "_precache_crispasr", noop_precache)
    monkeypatch.setattr(server, "_synthesize_wav_bytes", stuck_synth)

    async def main():
        async with server.lifespan(server.app):
            await cache.get_or_join("m1", "text", playback=False)  # 入 job
            await started.wait()  # worker 已進合成中（precache=False 下點播/直入仍走 worker）
        return server._http_client

    client = asyncio.run(main())
    assert client.is_closed  # shutdown 鏈完整


# --- 主控裁決 J5：統一入口涵蓋 MLX＋點播插隊＋暖機互斥 ---


def test_mlx_concurrent_playback_single_synthesis(monkeypatch, tmp_path):
    # J1：MLX 模式（_crispasr=None）同 reply 併發點播，經統一入口只合成一次——
    # 修前 MLX 端點直呼 tts_generate，同 reply 併發會合成兩次。
    box, cache = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "text")
    monkeypatch.setattr(server, "_crispasr", None)  # MLX 模式
    calls: list[str] = []
    started = asyncio.Event()
    gate = asyncio.Event()

    async def mlx_synth(text: str) -> bytes:
        calls.append(text)
        started.set()
        await gate.wait()
        return b"MLXWAV:" + text.encode("utf-8")

    async def main():
        cache.start(mlx_synth, precache=False)  # MLX：worker 運轉、不預合成

        async def hit():
            async with _asgi() as c:
                return await c.get("/voice/reply/m1/audio")

        t1 = asyncio.create_task(hit())
        await started.wait()  # 第一個點播已建 job、進合成
        t2 = asyncio.create_task(hit())  # 第二個點播撞 inflight、join
        await asyncio.sleep(0)
        gate.set()
        r1, r2 = await t1, await t2
        await cache.stop()
        return r1, r2

    r1, r2 = asyncio.run(main())
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == b"MLXWAV:text" and r2.content == b"MLXWAV:text"
    assert calls == ["text"]  # 只合成一次


def test_mlx_playback_timeout_returns_503(monkeypatch, tmp_path):
    # J1：MLX 模式點播 miss 也走統一入口＋REPLY_AUDIO_BUDGET 逾時（修前 MLX 直呼
    # tts_generate 無逾時預算）。
    box, cache = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "text")
    monkeypatch.setattr(server, "_crispasr", None)
    monkeypatch.setattr(server, "REPLY_AUDIO_BUDGET", 0.1)
    gate = asyncio.Event()

    async def mlx_synth(text: str) -> bytes:
        await gate.wait()
        return b"MLXWAV"

    async def main():
        cache.start(mlx_synth, precache=False)
        async with _asgi() as c:
            r = await c.get("/voice/reply/m1/audio")
        gate.set()
        await cache.stop()
        return r

    r = asyncio.run(main())
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "1"


def test_precache_and_worker_never_synth_concurrently(monkeypatch, tmp_path):
    # J3：暖機逐句取 _tts_lock，與 worker 合成序列化——峰值併發合成數=1。
    # 修前暖機不取鎖，與 worker 併發＝2。
    peak = 0

    async def main():
        nonlocal peak
        concurrent = 0
        lock = asyncio.Lock()
        monkeypatch.setattr(server, "_tts_lock", lock)
        monkeypatch.setattr(server, "TTS_ENGINE", "crispasr")

        async def synth(client, phrase):
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1
            return type("R", (), {"pcm": b"pcm"})()

        class FakeEngine:
            def __init__(self):
                self.synth = synth

            def pcm_to_wav(self, pcm):
                return b"wav"

            def is_unhealthy(self):
                return False

        monkeypatch.setattr(server, "_crispasr", FakeEngine())
        monkeypatch.setattr(server, "_http_client", object())
        cache = ReplyCache(tmp_path)
        cache.start(server._synthesize_wav_bytes, precache=True)
        await cache.enqueue("r1", "reply")  # worker 合成一筆
        await server._precache_crispasr()  # 暖機三句
        await cache.join()
        await cache.stop()

    asyncio.run(main())
    assert peak == 1  # 暖機與 worker 從不併發合成


# --- L1：MLX 同步阻塞經 to_thread 卸載，逾時準確且事件迴圈不凍 ---


async def _await_flag(flag: threading.Event, timeout: float = 2.0) -> bool:
    """以 async sleep 輪詢 threading.Event，不阻塞事件迴圈（供跨執行緒同步）。"""
    deadline = time.monotonic() + timeout
    while not flag.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return flag.is_set()


def test_mlx_sync_blocking_times_out_and_stays_responsive(monkeypatch, tmp_path):
    # L1：MLX 為同步 CPU 生成，_synthesize_wav_bytes 以 asyncio.to_thread 卸載到執行
    # 緒。用「同步 blocking」假引擎（time.sleep 迴圈、非可 await）驗：①小預算下端點
    # 準時 503（誤差有界）②合成阻塞期間並行打 /health 仍即時回應 ③引擎恰呼叫一次。
    #
    # 回歸守門：若把 MLX 改回迴圈內同步執行（不走 to_thread），blocking 會凍住整個
    # 事件迴圈——第一個 GET 的 REPLY_AUDIO_BUDGET 計時器無法觸發＝503 遲到，且並行
    # /health 卡住＝本測試 FAIL。
    box, cache = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "阻塞這段")
    monkeypatch.setattr(server, "TTS_ENGINE", "mlx")  # 非 crispasr → 走 MLX to_thread
    monkeypatch.setattr(server, "_crispasr", None)
    monkeypatch.setattr(server, "REPLY_AUDIO_BUDGET", 0.1)  # 極小預算逼準時逾時

    calls: list[str] = []
    entered = threading.Event()  # 合成執行緒已進入阻塞
    release = threading.Event()  # 主執行緒放行合成、避免長尾

    def blocking_mlx(text, **kwargs):
        calls.append(text)
        entered.set()
        # 同步阻塞（非 async）：未經 to_thread 就會凍住事件迴圈。time.sleep 短迴圈
        # 保 CPU 綁定阻塞形態，又可被主執行緒即時放行、不留 5s 長尾。
        deadline = time.monotonic() + 5.0
        while not release.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        return b"MLXWAV"

    monkeypatch.setattr(server, "_mlx_generate_wav", blocking_mlx)

    async def main():
        monkeypatch.setattr(server, "_tts_lock", asyncio.Lock())  # 綁當前 loop
        cache.start(server._synthesize_wav_bytes, precache=False)  # MLX：worker 運轉
        async with _asgi() as c:
            t0 = time.monotonic()
            r = await c.get("/voice/reply/m1/audio")  # 走 to_thread 阻塞 → 準時逾時
            reply_elapsed = time.monotonic() - t0
            assert await _await_flag(entered)  # 合成執行緒確實已進入同步阻塞
            h0 = time.monotonic()
            h = await c.get("/health")  # 合成阻塞期間並行打 → 事件迴圈未凍才會即時回
            health_elapsed = time.monotonic() - h0
        release.set()  # 放行合成執行緒
        await cache.stop()
        return r, reply_elapsed, h, health_elapsed

    r, reply_elapsed, h, health_elapsed = asyncio.run(main())
    assert r.status_code == 503  # ①準時逾時
    assert r.headers.get("retry-after") == "1"
    assert reply_elapsed < 0.5  # 誤差有界（預算 0.1）；inline 執行會遠超此界
    assert h.status_code == 200  # ②阻塞期間 /health 即時回應
    assert health_elapsed < 0.5  # 事件迴圈未被凍住的證據
    assert calls == ["阻塞這段"]  # ③引擎恰呼叫一次


# --- L2：/tts 與 reply worker 共用同一 MLX 核心，參數完整不漂移 ---


def test_tts_and_worker_share_mlx_core_with_full_params(monkeypatch, tmp_path):
    # L2：/tts 與 reply worker 的 MLX 路徑共用同一核心 _mlx_generate_wav（K3）。spy
    # 該核心，斷言：①/tts 路徑完整傳入 voice／temperature／speed／seed／lang／stream
    # ②reply worker 的 MLX 路徑呼叫同一核心函式（patch 一處、兩路皆命中＝共用，參數
    # 不漂移）。
    box, cache = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "worker 這段")
    monkeypatch.setattr(server, "TTS_ENGINE", "mlx")
    monkeypatch.setattr(server, "_crispasr", None)

    calls: list[tuple[str, dict]] = []

    def spy_core(text, **kwargs):
        calls.append((text, dict(kwargs)))
        return b"WAV"

    monkeypatch.setattr(server, "_mlx_generate_wav", spy_core)

    async def main():
        monkeypatch.setattr(server, "_tts_lock", asyncio.Lock())  # 綁當前 loop
        # /tts 路徑：對外六參完整傳入
        async with _asgi() as c:
            rt = await c.post("/tts", json={
                "text": "tts 這段", "voice": "Aria", "temperature": 0.7,
                "speed": 1.25, "seed": 99, "lang": "english", "stream": True,
            })
        # worker 路徑：get_or_join → worker → _synthesize_wav_bytes → 同一 spy
        cache.start(server._synthesize_wav_bytes, precache=False)
        fut = await cache.get_or_join("m1", "worker 這段", playback=True)
        wav = await asyncio.wait_for(fut, timeout=2.0)
        await cache.stop()
        return rt, wav

    rt, wav = asyncio.run(main())
    assert rt.status_code == 200
    assert wav == b"WAV"
    assert len(calls) == 2  # 兩路都命中同一 spy＝共用核心

    tts_text, tts_kw = calls[0]
    assert tts_text == "tts 這段"
    assert tts_kw == {  # ①/tts 完整六參，逐一到位
        "voice": "Aria", "temperature": 0.7, "speed": 1.25,
        "seed": 99, "lang": "english", "stream": True,
    }

    worker_text, worker_kw = calls[1]
    assert worker_text == "worker 這段"
    assert worker_kw == {  # ②worker 傳模組預設聲線參數，與 /tts 同核心、不漂移
        "voice": server.TTS_VOICE, "temperature": server.TTS_TEMPERATURE,
        "speed": server.TTS_SPEED, "seed": server.TTS_SEED,
    }
