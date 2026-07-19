"""入匣預合成音訊快取（票 6-3）。

回覆入匣即非同步觸發 TTS 合成，把 WAV 落到本地快取檔；PWA 點播命中快取＝直接
回檔（首音 <1s），未命中＝併入同一合成 job 等其結果。快取檔生命週期與回覆匣
一致——回覆被清出匣（到期／超窗）時，reply_box 的 on_delete hook 連動呼叫本模組
delete，快取不殘留。

合成入口統一（裁決 H1／J1）
--------------------------
所有合成——背景預合成與點播 miss、CrispASR 與 MLX——都收進 `get_or_join` 這唯一
入口，共享同一 `_inflight` job 表與單一 worker：

* 同一 reply 全域至多一個合成 job（撞 inflight 回同一 Future，去重）。
* 呼叫端只 await Future、永不直接呼叫引擎，故所有路徑共用「唯一發布守衛」與
  tombstone 保護。

佇列＝自管雙 deque＋單一 Condition（裁決 K2）
--------------------------------------------
`_hi`（點播）與 `_lo`（預合成）兩段 deque，worker 先消費 `_hi`。點播撞到「尚未
開始、仍在 `_lo`」的 job 時，把該 job 從 `_lo` 移到 `_hi`（promotion）——不吃新
槽位、無 stale item、無 QueueFull 失效路徑；容量以兩段總和判定。這是原「兩段
佇列」裁決的落地（PriorityQueue 等價替代已收回：其催促項要吃一個槽位，滿載時
`pass` 掉＝插隊恰好在最需要時失效）。

發布守衛（唯一一處，G1）
------------------------
worker 合成完成後，在「無 await 的同步段」內驗 tombstone——即 reply_box.on_delete
連動設下的「匣已刪」信號——有效才 os.replace 發布並 resolve Future。刪除進行中的
reply 會 cancel 其 Future，點播 await 端據此回 410。

原子發布
--------
先寫 `<key>.part`、fsync，再 os.replace 成 `<key>.wav`（同目錄 rename 為原子）。
任何中途失敗只留 .part，永不出現半成品 .wav。

殘留清理
--------
worker 佇列是記憶體態，進程結束即消。磁碟殘留（crash 遺留的 .part 半成品、對應
reply 已不在匣的孤兒 .wav）由 server 啟動時 sweep_orphans 一次清除。

檔名以 reply_id 的 sha256 為 key，避開特殊字元路徑問題（無法反推 reply_id，故
孤兒判斷比對 valid_ids 的 key 集合）。
"""

import asyncio
import hashlib
import logging
import os
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger("meowvoice-audio")

DEFAULT_MAX_QUEUE = 32
_PART_SUFFIX = ".part"
_WAV_SUFFIX = ".wav"

# 合成一段文字為完整 WAV bytes。由 server 注入（包住 TTS 引擎與 http client），
# 本模組不依賴任何具體引擎，測試以 fake 注入。
SynthFn = Callable[[str], Awaitable[bytes]]


class ReplyCacheFull(RuntimeError):
    """點播 job 入列時佇列已滿——無法即時合成，端點回 503 Retry-After。"""


def _swallow_unretrieved(fut: "asyncio.Future") -> None:
    """done callback：消費未被 await 的例外。

    預合成是 fire-and-forget（reply-callback 不 await Future），失敗時 Future 帶
    例外卻無人取，會觸發 asyncio 的 "exception never retrieved" 警告。此 callback
    在 Future 完成時主動取一次例外標記已取；若點播 join 了同一 Future，await 仍會
    照常 re-raise（取例外不清除結果，只清警告旗標）。"""
    if not fut.cancelled():
        fut.exception()


class _Job:
    """一個合成 job。lane 記錄它現在在哪一段佇列，供 promotion 與 stale 判斷。"""

    __slots__ = ("reply_id", "text", "fut", "lane")

    def __init__(self, reply_id: str, text: str, fut: "asyncio.Future[bytes]") -> None:
        self.reply_id = reply_id
        self.text = text
        self.fut = fut
        self.lane = "lo"  # "lo"（預合成段）｜"hi"（點播段）｜"running"（worker 處理中）


class ReplyCache:
    """預合成音訊快取＋統一合成佇列。

    生命週期：start() 啟動 worker（須在 running event loop 內）；stop() 取消 worker
    並喚醒殘留等待者。合成一律經 get_or_join；查詢／落檔／刪除為同步操作。
    """

    def __init__(self, cache_dir, *, max_queue: int = DEFAULT_MAX_QUEUE) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_queue = max_queue
        self._hi: "deque[_Job]" = deque()  # 點播段（優先消費）
        self._lo: "deque[_Job]" = deque()  # 預合成段
        self._cond = asyncio.Condition()
        self._worker: asyncio.Task | None = None
        self._synth_fn: SynthFn | None = None
        # reply_id → job。單一真相：預合成與點播共用同一 job／Future（去重）。
        self._inflight: dict[str, _Job] = {}
        # tombstone：合成期間被刪除的 reply_id（reply_box.on_delete 連動信號）。
        # worker 唯一的發布守衛據此放棄 os.replace，避免刪後快取復活（G1）。
        self._cancelled: set[str] = set()
        self._precache_enabled = True  # start(precache=) 設定；MLX 模式關閉入匣預合成

    # --- 生命週期 ---

    def start(self, synth_fn: SynthFn, *, precache: bool = True) -> None:
        """啟動背景 worker（lifespan startup 呼叫）。重複 start 為 no-op。

        precache=False（MLX 模式）：worker 照常運轉、供點播 miss 走統一入口，但入匣
        不排預合成 job（enqueue 回 disabled）——「不預合成」但點播 miss 仍經 worker
        與唯一發布守衛（J1）。"""
        if self._worker is not None:
            return
        self._synth_fn = synth_fn
        self._precache_enabled = precache
        self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """取消 worker、喚醒殘留等待者（lifespan shutdown 呼叫）。

        先 cancel＋await worker 收束（其 finally 跑完），再清兩段佇列與 inflight 並
        cancel 各 Future，避免點播端點永遠掛著。未 start 為 no-op；exception-safe，
        不讓 shutdown 後續 aclose 被打斷（G2）。

        清理須在 Condition 鎖內執行並 notify_all（L3）：join() 等待者可能正停在
        cond.wait()，而 worker 已被取消、不會再回迴圈頂 notify。唯有持鎖清空 inflight
        與兩段佇列後 notify_all，等待者才會醒來重評 while 條件並退出；否則它在
        Condition.wait() 永久卡住。"""
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("reply-cache worker 收束異常：%s", e)
        async with self._cond:
            for job in list(self._hi) + list(self._lo) + list(self._inflight.values()):
                if not job.fut.done():
                    job.fut.cancel()
            self._hi.clear()
            self._lo.clear()
            self._inflight.clear()
            self._cancelled.clear()
            self._cond.notify_all()

    # --- 合成入口（統一，H1／J1）---

    async def get_or_join(
        self, reply_id: str, text: str, *, playback: bool = False
    ) -> "asyncio.Future[bytes]":
        """取得該 reply 的合成結果 Future——合成的唯一入口。

        ①已有快取檔→回立即完成的 Future ②有進行中的 job→回同一 Future（去重；
        playback 且該 job 尚未開始時把它從 _lo 提升到 _hi，插隊到更早的低優先項
        之前，K2）③皆無→建新 job（playback 進 _hi、否則 _lo）。呼叫端只 await
        Future、永不直接呼叫引擎。

        佇列滿：點播 job 回帶 ReplyCacheFull 例外的 Future（端點轉 503）；預合成 job
        回已取消的 Future（＝skipped）。worker 未啟（MLX 模式）回已取消的 Future。"""
        loop = asyncio.get_running_loop()
        path = self._final_path(reply_id)
        if path.exists():
            fut: asyncio.Future[bytes] = loop.create_future()
            fut.set_result(path.read_bytes())
            return fut
        async with self._cond:
            existing = self._inflight.get(reply_id)
            if existing is not None:
                # K2 promotion：撞到尚未開始、仍在 _lo 的 job → 移到 _hi，讓 worker
                # 先消費該 reply（先於更早排入的 _lo 項）。不吃新槽位、無 stale item。
                if playback and existing.lane == "lo":
                    self._lo.remove(existing)
                    existing.lane = "hi"
                    self._hi.append(existing)
                    self._cond.notify_all()
                return existing.fut
            if self._worker is None:
                fut = loop.create_future()
                fut.cancel()  # worker 未啟（MLX 模式不預合成）
                return fut
            if len(self._hi) + len(self._lo) >= self._max_queue:
                fut = loop.create_future()
                fut.add_done_callback(_swallow_unretrieved)
                if playback:
                    fut.set_exception(ReplyCacheFull(reply_id))
                else:
                    logger.warning(
                        "reply-cache 佇列滿載，跳過預合成（回覆已入匣、不阻塞）：reply_id=%s", reply_id
                    )
                    fut.cancel()
                return fut
            self._cancelled.discard(reply_id)  # 新 job：清除上一輪殘留 tombstone
            fut = loop.create_future()
            fut.add_done_callback(_swallow_unretrieved)
            job = _Job(reply_id, text, fut)
            if playback:
                job.lane = "hi"
                self._hi.append(job)
            else:
                self._lo.append(job)
            self._inflight[reply_id] = job
            self._cond.notify_all()
            return fut

    async def enqueue(self, reply_id: str, text: str) -> str:
        """fire-and-forget 預合成登記（reply-callback 用），回狀態字串供日誌。
        內部走 get_or_join——與點播共用單一 job 生命週期。"""
        if self._worker is None:
            return "disabled"
        if not self._precache_enabled:
            return "disabled"  # MLX 模式不預合成（點播 miss 仍走 get_or_join）
        if self._final_path(reply_id).exists():
            return "cached"
        async with self._cond:
            if reply_id in self._inflight:
                return "duplicate"
        fut = await self.get_or_join(reply_id, text, playback=False)
        return "skipped" if fut.cancelled() else "queued"

    async def join(self) -> None:
        """等所有已排入／處理中的 job 完成（測試同步用）。worker 每輪迴圈頂 notify，
        故此 wait 會在每個 job 出佇列與完成時被喚醒重評。"""
        async with self._cond:
            while self._hi or self._lo or self._inflight:
                await self._cond.wait()

    async def _run(self) -> None:
        while True:
            async with self._cond:
                # notify join：上一輪 job 已完成、inflight 已在其 finally 清除。
                self._cond.notify_all()
                while not self._hi and not self._lo:
                    await self._cond.wait()
                job = self._hi.popleft() if self._hi else self._lo.popleft()
                job.lane = "running"
            # 合成在鎖外，讓生產者（get_or_join／promotion）能同時入列。
            try:
                if not job.fut.done():  # delete 已 cancel＝丟棄不做工
                    await self._synthesize(job.reply_id, job.text, job.fut)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("reply-cache 合成失敗：reply_id=%s err=%s", job.reply_id, e)
            finally:
                self._inflight.pop(job.reply_id, None)
                self._cancelled.discard(job.reply_id)

    async def _synthesize(self, reply_id: str, text: str, fut: "asyncio.Future[bytes]") -> None:
        assert self._synth_fn is not None
        if self._final_path(reply_id).exists():
            # 併發保險：期間已落檔——把既有結果餵給等待者，不重合成。
            if not fut.done():
                fut.set_result(self._final_path(reply_id).read_bytes())
            return
        try:
            wav = await self._synth_fn(text)
        except Exception as e:
            if not fut.done():
                fut.set_exception(e)
            raise
        # 唯一發布守衛（G1）：無 await 同步段內驗 tombstone（＝匣已刪連動信號），
        # 有效才 os.replace＋resolve；點播因共用此 Future 自動受保護。
        if reply_id in self._cancelled:
            if not fut.done():
                fut.cancel()
            return
        self._atomic_write(reply_id, wav)
        if not fut.done():
            fut.set_result(wav)

    # --- 落檔／查詢／刪除 ---

    def put(self, reply_id: str, wav: bytes) -> None:
        """原子落快取（測試造 fixture／直接落檔用）。端點不呼叫——合成一律走 worker。"""
        self._atomic_write(reply_id, wav)

    def path_if_ready(self, reply_id: str) -> Path | None:
        """命中回快取檔路徑，未命中回 None。"""
        path = self._final_path(reply_id)
        return path if path.exists() else None

    def delete(self, reply_id: str) -> None:
        """刪除快取（含任何半成品）。掛在 reply_box.on_delete，隨匣清除連動。

        該 reply 若尚有進行中的合成 job：記 tombstone（worker 發布守衛據此放棄，
        避免刪後復活，G1），並 cancel 其 Future——喚醒 await 的點播端點回 410（H3）。
        job 仍留佇列，worker 取到時見 fut.done() 丟棄，不重複合成。"""
        job = self._inflight.get(reply_id)
        if job is not None:
            self._cancelled.add(reply_id)
            if not job.fut.done():
                job.fut.cancel()
        for path in (self._final_path(reply_id), self._tmp_path(reply_id)):
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("reply-cache 刪除失敗：path=%s err=%s", path, e)

    def sweep_orphans(self, valid_ids) -> int:
        """啟動清理殘留，回刪除檔數。①所有 .part 半成品無條件刪；②reply_id 已
        不在匣的孤兒 .wav 刪。valid_ids 為目前匣內全部 reply_id。"""
        valid_keys = {self._key(rid) for rid in valid_ids}
        removed = 0
        for entry in self._dir.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            if name.endswith(_PART_SUFFIX):
                entry.unlink(missing_ok=True)  # crash 遺留的半成品
                removed += 1
            elif name.endswith(_WAV_SUFFIX) and entry.stem not in valid_keys:
                entry.unlink(missing_ok=True)  # 對應 reply 已不在匣的孤兒
                removed += 1
        if removed:
            logger.info("reply-cache 啟動清理殘留：removed=%d", removed)
        return removed

    # --- 內部 ---

    @staticmethod
    def _key(reply_id: str) -> str:
        return hashlib.sha256(reply_id.encode("utf-8")).hexdigest()

    def _final_path(self, reply_id: str) -> Path:
        return self._dir / (self._key(reply_id) + _WAV_SUFFIX)

    def _tmp_path(self, reply_id: str) -> Path:
        return self._dir / (self._key(reply_id) + _PART_SUFFIX)

    def _atomic_write(self, reply_id: str, wav: bytes) -> None:
        # 寫 .part → fsync → os.replace 成 .wav：合成完成前絕不出現可見的 .wav。
        tmp = self._tmp_path(reply_id)
        with open(tmp, "wb") as f:
            f.write(wav)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._final_path(reply_id))
