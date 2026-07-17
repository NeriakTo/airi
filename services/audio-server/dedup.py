"""語音 dispatch 冪等快取。

iOS Safari 在鎖屏時凍結 PWA 並回收連線；若 dispatch POST 的回應尚未被頁面
消費，喚醒後網路層可能在新連線上重放同一個 POST（2026-07-17 實案：同一句
語音間隔 17 分鐘注入兩次）。無法控制 Safari 行為，故以伺服器端冪等保證
「同一句話絕不注入兩次」。

兩層去重：
- client_msg_id（前端每句語音產生的 UUID）：TTL 需覆蓋長時間鎖屏，預設 30 分鐘。
- 文字雜湊 fallback（僅在無 client_msg_id 的舊客戶端請求時啟用）：窗口刻意
  縮短為 60 秒——使用者間隔一分鐘以上重講同一句話是合法操作，不可誤殺。
"""

import hashlib
import time


class DedupCache:
    def __init__(self, id_ttl: float = 1800.0, text_ttl: float = 60.0, max_entries: int = 256):
        self._id_ttl = id_ttl
        self._text_ttl = text_ttl
        self._max_entries = max_entries
        self._by_id: dict[str, tuple[dict, float]] = {}
        self._by_text: dict[str, tuple[dict, float]] = {}

    @staticmethod
    def _text_key(text: str) -> str:
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

    def _evict(self, now: float) -> None:
        for cache, ttl in ((self._by_id, self._id_ttl), (self._by_text, self._text_ttl)):
            for key in [k for k, (_, ts) in cache.items() if now - ts > ttl]:
                del cache[key]
            # 容量保險：正常語音頻率遠低於上限，超過代表異常，砍最舊的即可
            while len(cache) > self._max_entries:
                del cache[min(cache, key=lambda k: cache[k][1])]

    def lookup(self, client_msg_id: str, text: str, now: float | None = None) -> dict | None:
        """命中回傳首次 dispatch 的結果（呼叫端據此直接回應、不再注入）。"""
        now = time.time() if now is None else now
        self._evict(now)
        if client_msg_id:
            hit = self._by_id.get(client_msg_id)
            return hit[0] if hit else None
        hit = self._by_text.get(self._text_key(text))
        return hit[0] if hit else None

    def store(self, client_msg_id: str, text: str, result: dict, now: float | None = None) -> None:
        """只記成功注入的結果——失敗的 dispatch 理應允許重試。"""
        now = time.time() if now is None else now
        if client_msg_id:
            self._by_id[client_msg_id] = (result, now)
        self._by_text[self._text_key(text)] = (result, now)
