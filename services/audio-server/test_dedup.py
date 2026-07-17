"""dedup.py 單元測試。

重現 2026-07-17 實案：同一句語音「請整理記憶後重啟 session」在 12:47 與
13:04（間隔 17 分鐘）被注入兩次——iOS 鎖屏喚醒後重放 dispatch POST。
去重快取必須在 30 分鐘 TTL 內攔下同 client_msg_id 的重放，同時不誤殺
使用者刻意重講的同一句話（不同 client_msg_id）。

執行：.venv/bin/python -m pytest services/audio-server/test_dedup.py -q
"""

from dedup import DedupCache

RESULT = {"injected": True, "message_id": "voice-123-1", "elapsed": 0.1}


def test_same_client_msg_id_suppressed_across_17_minutes():
    # ROOT CAUSE 重現：實案的兩次注入間隔 17 分鐘（1020 秒），
    # 去重 TTL（30 分鐘）必須覆蓋這個鎖屏窗口。
    cache = DedupCache()
    cache.store("uuid-a", "請整理記憶後重啟 session", RESULT, now=1000.0)
    hit = cache.lookup("uuid-a", "請整理記憶後重啟 session", now=1000.0 + 1020.0)
    assert hit == RESULT


def test_id_expires_after_ttl():
    cache = DedupCache(id_ttl=1800.0)
    cache.store("uuid-a", "測試", RESULT, now=1000.0)
    assert cache.lookup("uuid-a", "測試", now=1000.0 + 1801.0) is None


def test_different_ids_same_text_not_suppressed():
    # 使用者刻意重講同一句話（前端每句都產生新 UUID）——不可誤殺
    cache = DedupCache()
    cache.store("uuid-a", "重啟 session", RESULT, now=1000.0)
    assert cache.lookup("uuid-b", "重啟 session", now=1030.0) is None


def test_legacy_client_without_id_uses_text_window():
    # 舊客戶端（無 client_msg_id）退回文字雜湊 60 秒窗口
    cache = DedupCache(text_ttl=60.0)
    cache.store("", "測試指令", RESULT, now=1000.0)
    assert cache.lookup("", "測試指令", now=1030.0) == RESULT
    assert cache.lookup("", "測試指令", now=1061.0) is None


def test_text_window_ignores_whitespace_diff():
    cache = DedupCache()
    cache.store("", "  測試指令 ", RESULT, now=1000.0)
    assert cache.lookup("", "測試指令", now=1010.0) == RESULT


def test_failed_dispatch_not_cached_allows_retry():
    # store 只在成功注入後被呼叫——這裡驗證未 store 的 id 不會被攔
    cache = DedupCache()
    assert cache.lookup("uuid-never-stored", "任何文字", now=1000.0) is None


def test_capacity_evicts_oldest():
    cache = DedupCache(max_entries=3, id_ttl=99999.0)
    for i in range(5):
        cache.store(f"uuid-{i}", f"text-{i}", RESULT, now=1000.0 + i)
    # 觸發 evict 需要一次 lookup
    assert cache.lookup("uuid-0", "text-0", now=1010.0) is None
    assert cache.lookup("uuid-4", "text-4", now=1010.0) == RESULT
