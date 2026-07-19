"""reply_box.py 單元測試（票 6-2）。

覆蓋狀態機全路徑、滾動窗物理刪除、重啟一致性、撞 ID 拒絕、清除 hook、
legacy 取件自動 ACK，以及與 dedup.py 協同不互踩。時鐘全程注入，不依賴真實
sleep。

執行：.venv/bin/python -m pytest services/audio-server/test_reply_box.py -q
"""

from dedup import DedupCache
from reply_box import (
    DEFAULT_WINDOW_SECONDS,
    AckOutcome,
    ReplyBox,
    ReplyState,
)


def _box(tmp_path, **kw) -> ReplyBox:
    return ReplyBox(tmp_path / "reply_box.db", **kw)


# --- 狀態遷移全路徑 ---


def test_enqueue_starts_delivered(tmp_path):
    box = _box(tmp_path)
    assert box.enqueue("r1", "你好", now=1000.0) is True
    assert box.get("r1", now=1000.0) == ("你好", ReplyState.DELIVERED)


def test_monotonic_forward_transitions(tmp_path):
    box = _box(tmp_path)
    box.enqueue("r1", "文字", now=1000.0)
    assert box.ack("r1", ReplyState.READ, now=1001.0) is AckOutcome.ADVANCED
    assert box.ack("r1", ReplyState.PLAYED, now=1002.0) is AckOutcome.ADVANCED
    assert box.get("r1", now=1002.0)[1] is ReplyState.PLAYED


def test_skip_level_transition_allowed(tmp_path):
    # delivered→played 跳過 read 是合法前進（legacy 取件即走此路）
    box = _box(tmp_path)
    box.enqueue("r1", "文字", now=1000.0)
    assert box.ack("r1", ReplyState.PLAYED, now=1001.0) is AckOutcome.ADVANCED
    assert box.get("r1", now=1001.0)[1] is ReplyState.PLAYED


def test_duplicate_ack_is_idempotent_noop(tmp_path):
    box = _box(tmp_path)
    box.enqueue("r1", "文字", now=1000.0)
    box.ack("r1", ReplyState.READ, now=1001.0)
    # 重複同階 ACK＝no-op 回成功（非例外），狀態不變
    assert box.ack("r1", ReplyState.READ, now=1002.0) is AckOutcome.UNCHANGED
    assert box.get("r1", now=1002.0)[1] is ReplyState.READ


def test_out_of_order_low_ack_does_not_regress(tmp_path):
    # 守門測試（見回報「守門自證」）：到 played 後收到亂序低階 read ACK，
    # 高階態不得被覆蓋。若實作退回無條件 UPDATE，本測試會 FAIL。
    box = _box(tmp_path)
    box.enqueue("r1", "文字", now=1000.0)
    box.ack("r1", ReplyState.PLAYED, now=1001.0)
    assert box.ack("r1", ReplyState.READ, now=1002.0) is AckOutcome.UNCHANGED
    assert box.get("r1", now=1002.0)[1] is ReplyState.PLAYED


def test_ack_regression_to_delivered_rejected(tmp_path):
    # 非法遷移拒絕：played→delivered（回退）不得發生。
    box = _box(tmp_path)
    box.enqueue("r1", "文字", now=1000.0)
    box.ack("r1", ReplyState.PLAYED, now=1001.0)
    assert box.ack("r1", ReplyState.DELIVERED, now=1002.0) is AckOutcome.UNCHANGED
    assert box.get("r1", now=1002.0)[1] is ReplyState.PLAYED


def test_ack_unknown_id_reports_not_found(tmp_path):
    box = _box(tmp_path)
    assert box.ack("nope", ReplyState.READ, now=1000.0) is AckOutcome.NOT_FOUND


def test_illegal_ack_target_rejected_before_db(tmp_path):
    # F1：非三態目標態於觸 DB 前拋 ValueError，不落庫污染。若無此守衛，
    # ack("r1", 3) 會寫 state=3，後續 get() 在 ReplyState(3) 反序列化時炸掉。
    box = _box(tmp_path)
    box.enqueue("r1", "文字", now=1000.0)
    for bad in (3, -1, "played"):
        try:
            box.ack("r1", bad, now=1001.0)
            assert False, f"非法 ACK 目標 {bad!r} 應拋 ValueError"
        except ValueError:
            pass
    # 狀態未被污染，get 仍能正常反序列化
    assert box.get("r1", now=1001.0)[1] is ReplyState.DELIVERED


# --- legacy 取件：自動 ACK 至 played、再取不重播 ---


def test_claim_auto_acks_to_played_then_no_replay(tmp_path):
    box = _box(tmp_path)
    box.enqueue("r1", "回覆內容", now=1000.0)
    assert box.claim_for_playback("r1", now=1001.0) == "回覆內容"
    assert box.get("r1", now=1001.0)[1] is ReplyState.PLAYED
    # 再取＝已 played，回 None（不重播），但資料仍留在匣中（供補聽端點/票 6-4）
    assert box.claim_for_playback("r1", now=1002.0) is None
    assert box.get("r1", now=1002.0) is not None


def test_claim_missing_returns_none(tmp_path):
    box = _box(tmp_path)
    assert box.claim_for_playback("ghost", now=1000.0) is None


# --- 鎖屏喚醒補取：TTL 5 分改滾動窗後的行為 ---


def test_locked_screen_wake_within_window_not_lost_not_replayed(tmp_path):
    # 舊行為：TTL 5 分，鎖屏 30 分喚醒即漏接。新行為：24h 滾動窗，30 分後仍取得。
    box = _box(tmp_path)
    box.enqueue("r1", "長任務結果", now=1000.0)
    thirty_min = 1000.0 + 30 * 60
    assert box.claim_for_playback("r1", now=thirty_min) == "長任務結果"  # 不漏
    assert box.claim_for_playback("r1", now=thirty_min + 1) is None  # 不重


# --- 撞 ID 拒絕並留痕 ---


def test_duplicate_reply_id_rejected(tmp_path, caplog):
    box = _box(tmp_path)
    assert box.enqueue("r1", "第一次", now=1000.0) is True
    with caplog.at_level("WARNING"):
        assert box.enqueue("r1", "第二次（重複投遞）", now=1001.0) is False
    # 不覆蓋既有內容
    assert box.get("r1", now=1001.0)[0] == "第一次"
    assert any("撞 ID" in rec.message for rec in caplog.records)


def test_empty_reply_id_rejected(tmp_path):
    box = _box(tmp_path)
    try:
        box.enqueue("", "文字", now=1000.0)
        assert False, "空 reply_id 應拋 ValueError"
    except ValueError:
        pass


# --- 到期與超窗物理刪除（24h／20 筆取小者）---


def test_expired_entries_physically_deleted(tmp_path):
    box = _box(tmp_path, max_entries=100)  # 隔離時間維度
    box.enqueue("old", "舊", now=1000.0)
    box.enqueue("fresh", "新", now=1000.0 + DEFAULT_WINDOW_SECONDS - 10)
    # 讓 old 逾 24h：查詢時 now 越過 old 的到期線
    at = 1000.0 + DEFAULT_WINDOW_SECONDS + 1
    assert box.get("old", now=at) is None
    assert box.get("fresh", now=at) is not None


def test_over_capacity_trimmed_to_newest_n(tmp_path):
    box = _box(tmp_path, max_entries=20)  # 隔離筆數維度（窗內全部同時段）
    for i in range(25):
        box.enqueue(f"r{i:02d}", f"文字{i}", now=1000.0 + i)
    assert box.count(now=1030.0) == 20
    assert box.get("r00", now=1030.0) is None  # 最舊 5 筆被砍
    assert box.get("r04", now=1030.0) is None
    assert box.get("r05", now=1030.0) is not None  # 最新 20 筆保留
    assert box.get("r24", now=1030.0) is not None


def test_window_takes_smaller_of_time_and_count(tmp_path):
    # 「取小者」：即使筆數未超 20，逾 24h 的仍先被時間維度砍掉
    box = _box(tmp_path, max_entries=20)
    box.enqueue("stale", "過期", now=0.0)
    box.enqueue("recent", "新鮮", now=DEFAULT_WINDOW_SECONDS + 100)
    assert box.count(now=DEFAULT_WINDOW_SECONDS + 100) == 1
    assert box.get("stale", now=DEFAULT_WINDOW_SECONDS + 100) is None


def test_expiry_boundary_inclusive_at_exactly_window(tmp_path):
    # F3：存活恰好滿 24h（整點）即刪；差一秒則保留。舊 `created_at < cutoff`
    # 在整點不刪，違反「逾期物理刪除」；改 `<=` 後整點即刪。
    box = _box(tmp_path, max_entries=100)  # 隔離時間維度
    box.enqueue("edge", "邊界", now=1000.0)
    assert box.get("edge", now=1000.0 + DEFAULT_WINDOW_SECONDS - 1) is not None  # 差一秒保留
    assert box.get("edge", now=1000.0 + DEFAULT_WINDOW_SECONDS) is None  # 恰好整點物理刪除


def test_sweep_purges_without_read_traffic(tmp_path):
    # F3：sweep 主動清理不依賴任何匣讀寫流量。用 on_delete hook 證明刪除確實
    # 發生在 sweep 當下（而非後續 get/count 順帶觸發），對應「無流量自主清理」。
    deleted: list[str] = []
    box = _box(tmp_path, on_delete=deleted.append)
    box.enqueue("r1", "逾期敏感內容", now=1000.0)
    assert deleted == []  # 入匣時未過期
    box.sweep(now=1000.0 + DEFAULT_WINDOW_SECONDS + 1)
    assert deleted == ["r1"]  # sweep 當下即物理刪除


def test_active_ids_lists_current_and_excludes_expired(tmp_path):
    # 票 6-3 startup 孤兒快取比對用：回現匣所有 id，且先清到期不列已逾期者。
    box = _box(tmp_path, max_entries=100)
    box.enqueue("old", "t", now=0.0)
    box.enqueue("new", "t", now=DEFAULT_WINDOW_SECONDS + 100)
    assert box.active_ids(now=DEFAULT_WINDOW_SECONDS + 100) == ["new"]


# --- 清除 hook（供票 6-3 快取檔連動刪除）---


def test_on_delete_hook_fires_for_expired_and_trimmed(tmp_path):
    deleted: list[str] = []
    box = _box(tmp_path, max_entries=2, on_delete=deleted.append)
    box.enqueue("a", "1", now=1000.0)
    box.enqueue("b", "2", now=1001.0)
    box.enqueue("c", "3", now=1002.0)  # 觸發超窗：a 被砍
    assert deleted == ["a"]
    # 到期也觸發 hook
    box.get("b", now=1002.0 + DEFAULT_WINDOW_SECONDS + 1)
    assert "b" in deleted


def test_on_delete_hook_failure_does_not_block_cleanup(tmp_path):
    def boom(_reply_id: str) -> None:
        raise RuntimeError("快取刪除炸了")

    box = _box(tmp_path, max_entries=1, on_delete=boom)
    box.enqueue("a", "1", now=1000.0)
    box.enqueue("b", "2", now=1001.0)  # a 超窗、hook 拋例外但清除仍完成
    assert box.get("a", now=1001.0) is None


# --- 重啟一致性（重建實例後狀態與內容一致）---


def test_restart_recovers_content_and_ack_state(tmp_path):
    db = tmp_path / "reply_box.db"
    box_a = ReplyBox(db)
    box_a.enqueue("r1", "重啟前寫入", now=1000.0)
    box_a.ack("r1", ReplyState.READ, now=1001.0)
    box_a.enqueue("r2", "另一筆", now=1002.0)
    box_a.close()  # 模擬 server 重啟

    box_b = ReplyBox(db)
    assert box_b.get("r1", now=1003.0) == ("重啟前寫入", ReplyState.READ)
    assert box_b.get("r2", now=1003.0) == ("另一筆", ReplyState.DELIVERED)


# --- 與 DedupCache 協同不互踩（入站冪等 vs 出站回覆匣）---


def test_dedup_and_reply_box_keyspaces_independent(tmp_path):
    # 即使 client_msg_id 與 reply_id 字串相同，兩者為不同儲存、互不干擾。
    shared_id = "voice-123-1"
    dedup = DedupCache()
    box = _box(tmp_path)
    dedup.store(shared_id, "指令原文", {"injected": True}, now=1000.0)
    box.enqueue(shared_id, "回覆原文", now=1000.0)
    # dedup 命中回入站結果；reply_box 回出站回覆——各自獨立
    assert dedup.lookup(shared_id, "指令原文", now=1000.0) == {"injected": True}
    assert box.get(shared_id, now=1000.0)[0] == "回覆原文"
    # 從 reply_box 取件不影響 dedup 的攔阻
    box.claim_for_playback(shared_id, now=1001.0)
    assert dedup.lookup(shared_id, "指令原文", now=1001.0) == {"injected": True}
