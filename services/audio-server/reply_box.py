"""語音回覆匣（持久化）＋ACK 三態狀態機。票 6-2。

取代舊 `_pending_replies` 記憶體暫存（pop 語意、TTL 5 分）。回覆入匣後不再
一取即消，改由 ACK 驅動生命週期，並以 SQLite 檔為單一真相——server 重啟後
匣內容與 ACK 態原樣復原。

狀態模型（全序、單調）：

    delivered(0) → read(1) → played(2)

- 入匣即 delivered。
- ACK 只前進不回退：目標態 > 現態＝前進（ADVANCED）；目標態 == 現態＝重複
  ACK，冪等 no-op（UNCHANGED）；目標態 < 現態＝亂序低階 ACK，不覆蓋高階態、
  狀態不變（UNCHANGED）——即「禁回退」。允許跳階（delivered→played，如 legacy
  取件直接標 played）。
- reply ID 全域唯一。入匣撞 ID＝拒絕並留痕、不覆蓋既有（Discord 與語音端共用
  同一 reply ID，重複投遞在此擋下＝防雙投）。

滾動窗＝「24 小時」與「最近 20 筆」的交集（取小者）：凡逾 24h 或排名超出最新
20 名即物理刪除。played 後不提前清，保留至到期供補聽。

持久層選 SQLite（非檔案型）：狀態機的單調遷移需條件式原子更新
（`UPDATE ... WHERE state < target`），滾動窗清理需原子多列刪除，SQLite 直接
提供 ACID 且重啟免重建；檔案型得自行處理鎖與 read-modify-write 競態，代價更高。

清除 hook：`on_delete(reply_id)` 於任何物理刪除（到期／超窗）時呼叫，供票 6-3
掛快取檔連動刪除；本票不實作快取，預設 None。

時鐘可注入（`clock` 或各方法的 `now`）以利測試不依賴真實 sleep。
"""

import logging
import sqlite3
import threading
import time
from enum import Enum, IntEnum
from pathlib import Path
from typing import Callable

logger = logging.getLogger("meowvoice-audio")

# 滾動窗預設：24 小時、最近 20 筆（取小者）。與票 6-4 未讀列表一頁 20 筆同界。
DEFAULT_WINDOW_SECONDS = 24 * 60 * 60
DEFAULT_MAX_ENTRIES = 20


class ReplyState(IntEnum):
    """回覆生命週期三態；數值即單調順序，大者為高階態。"""

    DELIVERED = 0
    READ = 1
    PLAYED = 2


class AckOutcome(Enum):
    """`ack()` 結果。三者皆非例外；NOT_FOUND 供呼叫端決定是否留痕。"""

    NOT_FOUND = "not_found"
    ADVANCED = "advanced"  # 狀態前進到更高階
    UNCHANGED = "unchanged"  # 重複 ACK 或亂序低階 ACK，冪等 no-op


class ReplyBox:
    """SQLite 持久化回覆匣。

    方法皆同步且以 `self._lock` 序列化（單一共享連線，check_same_thread=False）。
    server 於事件迴圈執行緒直接呼叫、方法內不 await，故實務上單執行緒；鎖為
    跨執行緒共享連線的廉價保險。每個公開方法自成一筆交易（單次 commit）。
    """

    def __init__(
        self,
        db_path,
        *,
        clock: Callable[[], float] = time.time,
        on_delete: Callable[[str], None] | None = None,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._on_delete = on_delete
        self._window_seconds = window_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        # WAL：新連線（＝重啟後的新實例）讀得到已 commit 的資料，重啟一致性靠此。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS replies ("
            " reply_id TEXT PRIMARY KEY,"  # 全域唯一，撞 ID 由 INSERT 的唯一約束擋下
            " text TEXT NOT NULL,"
            # CHECK 為第二層守：即便繞過 Python 驗證，非三態值也寫不進 DB（F1）。
            " state INTEGER NOT NULL CHECK(state IN (0, 1, 2)),"
            " created_at REAL NOT NULL)"
        )
        self._conn.commit()

    def close(self) -> None:
        """關閉連線（WAL 於最後連線關閉時 checkpoint）。測試模擬重啟前呼叫。"""
        with self._lock:
            self._conn.close()

    def enqueue(self, reply_id: str, text: str, now: float | None = None) -> bool:
        """入匣一筆新回覆（初始態 delivered）。成功回 True；撞 ID＝拒絕並留痕回 False。"""
        if not reply_id:
            raise ValueError("reply_id 不可為空")
        now = self._clock() if now is None else now
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO replies(reply_id, text, state, created_at) VALUES (?,?,?,?)",
                    (reply_id, text, int(ReplyState.DELIVERED), now),
                )
            except sqlite3.IntegrityError:
                # 撞 ID＝重複投遞（Discord 與語音端共用 reply ID）。不覆蓋既有、留痕。
                self._conn.rollback()
                logger.warning("reply-box 撞 ID，拒絕重複入匣：reply_id=%s", reply_id)
                return False
            # 入匣即為窗口成長的觸發點：先清到期、再砍超窗，一併原子 commit。
            self._enforce_window(now)
            self._conn.commit()
            return True

    def ack(self, reply_id: str, target: ReplyState, now: float | None = None) -> AckOutcome:
        """單調推進 ACK 態。回退／重複＝UNCHANGED（冪等）；僅前進回 ADVANCED。

        target 非三態＝非法遷移，於觸 DB 前拋 ValueError（fail-loud，F1）——
        避免非法 state 落庫後 get() 在 ReplyState() 反序列化時炸掉。"""
        if not isinstance(target, ReplyState):
            target = ReplyState(target)  # 非三態值在此拋 ValueError，不觸 DB
        now = self._clock() if now is None else now
        with self._lock:
            self._purge_expired(now)
            row = self._conn.execute(
                "SELECT state FROM replies WHERE reply_id=?", (reply_id,)
            ).fetchone()
            if row is None:
                outcome = AckOutcome.NOT_FOUND
            elif int(target) <= int(row[0]):
                # 禁回退＋重複冪等：同階或低階 ACK 不覆蓋現態。
                outcome = AckOutcome.UNCHANGED
            else:
                # 條件式原子更新：WHERE state < target 是並發下不回退的最終防線。
                self._conn.execute(
                    "UPDATE replies SET state=? WHERE reply_id=? AND state < ?",
                    (int(target), reply_id, int(target)),
                )
                outcome = AckOutcome.ADVANCED
            self._conn.commit()
            return outcome

    def claim_for_playback(self, reply_id: str, now: float | None = None) -> str | None:
        """legacy GET 取件語意：首次取件自動 ACK 至 played 並回文字；已 played
        （或不存在／已到期）回 None——再取不重播（防重播風暴）。

        單次交易內 SELECT+UPDATE 保證「標 played」與「回文字」原子成對，
        並發重放的第二次取件必見 played 態而回 None。"""
        now = self._clock() if now is None else now
        with self._lock:
            self._purge_expired(now)
            row = self._conn.execute(
                "SELECT text, state FROM replies WHERE reply_id=?", (reply_id,)
            ).fetchone()
            text: str | None = None
            if row is not None and int(row[1]) < int(ReplyState.PLAYED):
                self._conn.execute(
                    "UPDATE replies SET state=? WHERE reply_id=?",
                    (int(ReplyState.PLAYED), reply_id),
                )
                text = row[0]
            self._conn.commit()
            return text

    def get(self, reply_id: str, now: float | None = None) -> tuple[str, ReplyState] | None:
        """唯讀查詢，回 (text, state) 或 None（不改狀態）。到期會先物理刪除再查。"""
        now = self._clock() if now is None else now
        with self._lock:
            self._purge_expired(now)
            row = self._conn.execute(
                "SELECT text, state FROM replies WHERE reply_id=?", (reply_id,)
            ).fetchone()
            self._conn.commit()
            if row is None:
                return None
            return (row[0], ReplyState(row[1]))

    def count(self, now: float | None = None) -> int:
        """匣內筆數（先清到期再數，反映滾動窗生效後的真實筆數）。"""
        now = self._clock() if now is None else now
        with self._lock:
            self._purge_expired(now)
            n = self._conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
            self._conn.commit()
            return int(n)

    def active_ids(self, now: float | None = None) -> list[str]:
        """回目前匣內所有 reply_id（先清到期）。供票 6-3 startup 比對、清除
        對應 reply 已不在匣的孤兒快取檔。"""
        now = self._clock() if now is None else now
        with self._lock:
            self._purge_expired(now)
            rows = self._conn.execute("SELECT reply_id FROM replies").fetchall()
            self._conn.commit()
            return [r[0] for r in rows]

    def list_recent(
        self, now: float | None = None, limit: int | None = None
    ) -> list[tuple[str, str, ReplyState, float]]:
        """回滾動窗內項目，created_at 倒序（新→舊），供票 6-4 PWA 未讀補取列表。

        唯讀不改任何 ACK 態（read／played ACK 由 PWA 顯式回報，見端點）。先清到期
        （與其他讀方法一致，僅時間維度物理刪除），再取最新 limit 筆——limit 預設
        ＝滾動窗上限 max_entries（20），即「與滾動窗同界、一頁不分頁」。SQL 的
        LIMIT 是即使入匣期未及砍到超窗也絕不回超過一頁的最終防線。

        回 list[(reply_id, text, state, created_at)]，同刻以 reply_id DESC 為穩定
        次序（與 _trim_over_capacity 的保留排序同鍵，列表與清理視角一致）。"""
        now = self._clock() if now is None else now
        cap = self._max_entries if limit is None else limit
        with self._lock:
            self._purge_expired(now)
            rows = self._conn.execute(
                "SELECT reply_id, text, state, created_at FROM replies"
                " ORDER BY created_at DESC, reply_id DESC LIMIT ?",
                (cap,),
            ).fetchall()
            self._conn.commit()
            return [(r[0], r[1], ReplyState(r[2]), r[3]) for r in rows]

    def sweep(self, now: float | None = None) -> None:
        """主動全窗清理（清到期＋砍超窗），不依賴匣讀寫流量。

        server 啟動時清一次、並由週期任務定時呼叫：否則服務無流量期間，逾期
        敏感內容會滯留磁碟超過 24h，違反「留存即敏感面、逾期物理刪除」裁決（F3）。"""
        now = self._clock() if now is None else now
        with self._lock:
            self._enforce_window(now)
            self._conn.commit()

    # --- 內部清理（呼叫端已持鎖，不自行 commit，由公開方法統一 commit）---

    def _enforce_window(self, now: float) -> None:
        """滾動窗＝24h 與最近 max_entries 筆的交集：兩條約束各刪一次即為取小者。"""
        self._purge_expired(now)
        self._trim_over_capacity()

    def _purge_expired(self, now: float) -> None:
        cutoff = now - self._window_seconds
        # 邊界含等於：存活滿 window（恰好 24h 整點）即逾期物理刪除，不多留一瞬（F3）。
        expired = [
            r[0]
            for r in self._conn.execute(
                "SELECT reply_id FROM replies WHERE created_at <= ?", (cutoff,)
            ).fetchall()
        ]
        self._delete_ids(expired)

    def _trim_over_capacity(self) -> None:
        # 保留最新 max_entries 筆（created_at 大者為新，同刻以 reply_id 為穩定次序），
        # OFFSET 之後的舊筆物理刪除。
        stale = [
            r[0]
            for r in self._conn.execute(
                "SELECT reply_id FROM replies"
                " ORDER BY created_at DESC, reply_id DESC LIMIT -1 OFFSET ?",
                (self._max_entries,),
            ).fetchall()
        ]
        self._delete_ids(stale)

    def _delete_ids(self, ids: list[str]) -> None:
        for reply_id in ids:
            self._conn.execute("DELETE FROM replies WHERE reply_id=?", (reply_id,))
            if self._on_delete is not None:
                try:
                    # 票 6-3 於此連動刪除預合成快取檔；hook 失敗不得阻斷清除。
                    self._on_delete(reply_id)
                except Exception as e:
                    logger.warning(
                        "reply-box on_delete hook 失敗：reply_id=%s err=%s", reply_id, e
                    )
