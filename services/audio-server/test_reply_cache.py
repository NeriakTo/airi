"""reply_cache.py 單元測試（票 6-3）。

以 fake synth_fn（不打真引擎）＋tmp 快取目錄，涵蓋佇列契約全路徑：積壓全成、
同 reply 去重、佇列滿載跳過、原子發布無半成品、合成失敗降級不卡 worker、隨匣
清除連動、重啟殘留清理。worker 用 asyncio.Event 精準控制合成時機，不靠真實
sleep 賭時序。

執行：.venv/bin/python -m pytest services/audio-server/test_reply_cache.py -q
"""

import asyncio

from reply_box import ReplyBox
from reply_cache import ReplyCache


class FakeSynth:
    """可控 fake 合成器：記錄呼叫、可用 gate 阻塞、可對指定文字丟例外。

    started 於進入合成時 set——測試以 `await synth.started.wait()` 精確同步「worker
    已進合成」，取代 sleep(0.05) 的時序賭博（J4）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.fail_on: set[str] = set()
        self.started = asyncio.Event()

    async def __call__(self, text: str) -> bytes:
        self.calls.append(text)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if text in self.fail_on:
            raise RuntimeError("synth boom")
        return b"WAV:" + text.encode("utf-8")


# --- ① 連續 5 筆積壓全數落檔 ---


def test_five_backlog_all_synthesized(tmp_path):
    async def main():
        synth = FakeSynth()
        cache = ReplyCache(tmp_path)
        cache.start(synth)
        for i in range(5):
            assert await cache.enqueue(f"r{i}", f"text{i}") == "queued"
        await cache.join()  # 等 worker 處理完 5 筆
        await cache.stop()
        return synth, cache

    synth, cache = asyncio.run(main())
    for i in range(5):
        assert cache.path_if_ready(f"r{i}") is not None
    assert len(synth.calls) == 5


# --- ② 同 reply 至多一個合成 job（去重）---


def test_same_reply_deduped_single_job(tmp_path):
    async def main():
        synth = FakeSynth()
        synth.gate = asyncio.Event()  # 卡住 worker，讓兩次 enqueue 期間 r1 仍 inflight
        cache = ReplyCache(tmp_path)
        cache.start(synth)
        assert await cache.enqueue("r1", "hello") == "queued"
        await synth.started.wait()  # worker 取出 r1、進合成（精確同步，非時序 sleep）
        assert await cache.enqueue("r1", "hello") == "duplicate"
        synth.gate.set()
        await cache.join()
        await cache.stop()
        return synth

    synth = asyncio.run(main())
    assert synth.calls.count("hello") == 1  # 只合成一次


def test_already_cached_not_requeued(tmp_path):
    async def main():
        synth = FakeSynth()
        cache = ReplyCache(tmp_path)
        cache.start(synth)
        cache.put("r1", b"WAV")  # 已快取
        result = await cache.enqueue("r1", "hello")
        await cache.stop()
        return result, synth

    result, synth = asyncio.run(main())
    assert result == "cached"
    assert synth.calls == []


# --- ③ 佇列滿載跳過預合成、不阻塞 ---


def test_queue_full_skips_without_blocking(tmp_path):
    async def main():
        synth = FakeSynth()
        synth.gate = asyncio.Event()
        cache = ReplyCache(tmp_path, max_queue=1)
        cache.start(synth)
        assert await cache.enqueue("r0", "t0") == "queued"
        await synth.started.wait()  # worker 取出 r0、進合成（佇列空）
        r1 = await cache.enqueue("r1", "t1")  # 進佇列（滿 1）
        r2 = await cache.enqueue("r2", "t2")  # 佇列滿 → 跳過
        synth.gate.set()
        await cache.join()
        await cache.stop()
        return r1, r2

    r1, r2 = asyncio.run(main())
    assert r1 == "queued"
    assert r2 == "skipped"


def test_enqueue_before_start_is_disabled(tmp_path):
    # worker 未啟（如 MLX 模式）＝不預合成，回覆入匣不受影響。
    async def main():
        cache = ReplyCache(tmp_path)
        return await cache.enqueue("r1", "t")

    assert asyncio.run(main()) == "disabled"


# --- ④ 原子發布：合成完成前不可見半成品 ---


def test_no_visible_wav_while_synthesizing(tmp_path):
    async def main():
        synth = FakeSynth()
        synth.gate = asyncio.Event()
        cache = ReplyCache(tmp_path)
        cache.start(synth)
        await cache.enqueue("r1", "hello")
        await synth.started.wait()  # worker 進合成中
        mid_ready = cache.path_if_ready("r1")
        mid_wavs = list(tmp_path.glob("*.wav"))
        synth.gate.set()
        await cache.join()
        await cache.stop()
        return mid_ready, mid_wavs, cache

    mid_ready, mid_wavs, cache = asyncio.run(main())
    assert mid_ready is None  # 合成中不可見
    assert mid_wavs == []  # 目錄無半成品 .wav
    assert cache.path_if_ready("r1") is not None  # 完成後才出現


def test_put_leaves_no_part_residue(tmp_path):
    cache = ReplyCache(tmp_path)
    cache.put("r1", b"WAV")
    assert cache.path_if_ready("r1") is not None
    assert list(tmp_path.glob("*.part")) == []  # os.replace 後無 tmp 殘留


# --- ⑤ 合成失敗降級：不落檔、worker 續跑 ---


def test_synth_failure_no_file_and_worker_survives(tmp_path):
    async def main():
        synth = FakeSynth()
        synth.fail_on = {"bad"}
        cache = ReplyCache(tmp_path)
        cache.start(synth)
        await cache.enqueue("r_bad", "bad")
        await cache.enqueue("r_ok", "good")
        await cache.join()
        await cache.stop()
        return cache

    cache = asyncio.run(main())
    assert cache.path_if_ready("r_bad") is None  # 失敗不落檔
    assert cache.path_if_ready("r_ok") is not None  # worker 未卡死，續處理下一筆


# --- ⑥ 快取隨匣清除連動（reply_box.on_delete → cache.delete）---


def test_delete_removes_final_and_part(tmp_path):
    cache = ReplyCache(tmp_path)
    cache.put("r1", b"WAV")
    (tmp_path / (ReplyCache._key("r1") + ".part")).write_bytes(b"partial")  # 造殘留
    cache.delete("r1")
    assert cache.path_if_ready("r1") is None
    assert list(tmp_path.iterdir()) == []


def test_reply_box_purge_triggers_cache_delete(tmp_path):
    # 生命週期一致：超窗清出匣 → on_delete → 快取檔連動刪除。
    cache = ReplyCache(tmp_path / "cache")
    box = ReplyBox(tmp_path / "box.db", on_delete=cache.delete, max_entries=1)
    box.enqueue("a", "ta", now=1000.0)
    cache.put("a", b"WAV-A")
    assert cache.path_if_ready("a") is not None
    box.enqueue("b", "tb", now=1001.0)  # a 超窗 → on_delete("a")
    assert cache.path_if_ready("a") is None


# --- ⑦ 重啟殘留清理：半成品 .part ＋孤兒 .wav ---


def test_sweep_orphans_removes_part_and_orphan_wav(tmp_path):
    cache = ReplyCache(tmp_path)
    cache.put("keep", b"WAV")  # 在匣
    cache.put("orphan", b"WAV")  # 不在匣
    (tmp_path / (ReplyCache._key("half") + ".part")).write_bytes(b"partial")  # crash 半成品
    removed = cache.sweep_orphans(valid_ids=["keep"])
    assert removed == 2  # orphan.wav + half.part
    assert cache.path_if_ready("keep") is not None
    assert cache.path_if_ready("orphan") is None
    assert list(tmp_path.glob("*.part")) == []


# --- 競態修復（黑喵 R1）---


def test_delete_during_synthesis_prevents_revival(tmp_path):
    # G1：合成期間 reply 被刪（到期／超窗），worker 完成後不得再發布快取而復活。
    async def main():
        synth = FakeSynth()
        synth.gate = asyncio.Event()
        cache = ReplyCache(tmp_path)
        cache.start(synth)
        await cache.enqueue("r1", "hello")
        await synth.started.wait()  # worker 取出、進合成中
        cache.delete("r1")  # 匣清除連動刪除 → 記 tombstone
        synth.gate.set()  # worker 合成完成，嘗試發布
        await cache.join()
        await cache.stop()
        return cache

    cache = asyncio.run(main())
    assert cache.path_if_ready("r1") is None  # 不復活
    assert list(tmp_path.glob("*.wav")) == []


def test_stop_during_synthesis_is_clean(tmp_path):
    # G2：合成中 shutdown 不得拋（舊 bug：先清 _queue 再取消，worker finally 對 None
    # 呼叫 task_done 拋 AttributeError，中斷 lifespan 後續 aclose）。
    async def main():
        synth = FakeSynth()
        synth.gate = asyncio.Event()  # 永不 set：worker 卡合成中被 stop 取消
        cache = ReplyCache(tmp_path)
        cache.start(synth)
        await cache.enqueue("r1", "hello")
        await synth.started.wait()  # worker 已進合成中（隨後被 stop 取消）
        await cache.stop()  # 不得拋
        return True

    assert asyncio.run(main()) is True


def test_playback_preempts_earlier_low_prio_and_no_double_synth(tmp_path):
    # J2：點播 join 尚未開始的低優先 job，須先於更早排入的低優先項被合成；原 target
    # 低優先項當 stale 丟棄——同 reply 全域至多一次實際合成。修前（無催促項）worker
    # 依 seq 取 older 先於 target，calls 會是 ["b","o","t"]。
    async def main():
        synth = FakeSynth()
        synth.gate = asyncio.Event()
        cache = ReplyCache(tmp_path, max_queue=8)
        cache.start(synth)
        await cache.enqueue("blocker", "b")  # 佔住 worker（合成中）
        await synth.started.wait()  # worker 卡在 blocker
        await cache.enqueue("older", "o")  # 更早排入的低優先 job
        await cache.enqueue("target", "t")  # 稍晚排入的低優先 job
        await cache.get_or_join("target", "t", playback=True)  # 點播催促（高優先插隊）
        synth.gate.set()  # 放行——worker 續消費
        await cache.join()
        await cache.stop()
        return synth.calls

    calls = asyncio.run(main())
    assert calls.index("t") < calls.index("o")  # target 插隊到 older 之前
    assert calls.count("t") == 1  # 原 target 低優先項當 stale，不重複合成
    assert calls == ["b", "t", "o"]


def test_promotion_survives_full_queue(tmp_path):
    # K2/K4②：佇列填滿時點播仍能插隊——promotion 把 job 從 lo 移到 hi、不吃新槽位、
    # 無 QueueFull 失效路徑。修前（PriorityQueue 催促項吃一槽，滿載時 pass 掉）會在
    # 最需要插隊時退化成 blocker→older→target。
    async def main():
        synth = FakeSynth()
        synth.gate = asyncio.Event()
        cache = ReplyCache(tmp_path, max_queue=2)
        cache.start(synth)
        await cache.enqueue("blocker", "b")  # 佔住 worker（合成中）
        await synth.started.wait()
        await cache.enqueue("older", "o")  # lo 槽 1
        await cache.enqueue("target", "t")  # lo 槽 2——佇列填滿（hi=0, lo=2）
        await cache.get_or_join("target", "t", playback=True)  # promotion，不吃新槽
        synth.gate.set()
        await cache.join()
        await cache.stop()
        return synth.calls

    calls = asyncio.run(main())
    assert calls.index("t") < calls.index("o")  # 滿載下 target 仍插隊到 older 前
    assert calls == ["b", "t", "o"]


# --- stop() 收束（L3）：清理須持 Condition 鎖＋notify_all，否則 join() 卡死 ---


def test_stop_idle_worker_is_clean_and_reentrant(tmp_path):
    # L3①：worker 閒置（無 job、停在 cond.wait()）時 stop 乾淨收束；重入為 no-op。
    async def main():
        synth = FakeSynth()  # 不會被呼叫（無 job）
        cache = ReplyCache(tmp_path)
        cache.start(synth)
        await asyncio.sleep(0)  # 讓 worker 起身、停在等 job 的 cond.wait()
        await asyncio.wait_for(cache.stop(), timeout=1.0)  # 乾淨收束、不卡死
        await asyncio.wait_for(cache.stop(), timeout=1.0)  # 重入 no-op
        return synth
    synth = asyncio.run(main())
    assert synth.calls == []


def test_stop_wakes_blocked_join_waiter(tmp_path):
    # L3②：有 join() 等待中時 stop 不得卡死。
    #
    # ROOT CAUSE：
    # join() 停在 `await self._cond.wait()`；worker 卡合成中被 stop 取消後，不會再
    # 回迴圈頂 notify。舊 stop 在鎖外清 inflight／佇列且不 notify_all，等待者永遠
    # 收不到喚醒＝在 Condition.wait() 永久卡住。
    # 修法：stop 的清理改在 Condition 鎖內執行並 notify_all，喚醒等待者重評 while。
    async def main():
        synth = FakeSynth()
        synth.gate = asyncio.Event()  # 永不 set：worker 卡合成中被 stop 取消
        cache = ReplyCache(tmp_path)
        cache.start(synth)
        await cache.enqueue("r1", "hello")
        await synth.started.wait()  # worker 已進合成、inflight 尚存 r1
        joiner = asyncio.create_task(cache.join())  # 停在 cond.wait()
        await asyncio.sleep(0)  # 讓 joiner 推進到 wait 點
        await asyncio.wait_for(cache.stop(), timeout=1.0)  # 須喚醒 joiner、自身不卡
        await asyncio.wait_for(joiner, timeout=1.0)  # join() 被喚醒後退出、不卡死
        return True
    assert asyncio.run(main()) is True
