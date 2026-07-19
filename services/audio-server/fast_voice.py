"""fast-voice runtime adapter — 灰喵（本機 llama.cpp）語音對話後端。

語音主模型規劃 v2（2026-07-17 定案）：對話類語音不再走主 session，改由
本機 llama.cpp（Qwen3.6-35B-A3B，thinking 必須關閉）套青喵 persona 直接
回覆，讓語音鏈路與主 session 模型解耦。任務類升級與故障 fallback 屬 P2，
本模組先把介面留在 generate 的錯誤路徑上。

協定：server.py 的 POST /runtime/fast-voice 收 text＋callback_url 後立即
回 message_id，推理在背景 task 完成後把回覆 POST 回 callback_url——對
PWA 與既有 runtime adapter（mcp-bridge）完全同形，前端零改動。
"""

import itertools
import json
import logging
import re
import time
from pathlib import Path

import httpx

logger = logging.getLogger("meowvoice-audio")

PERSONA_FILE = Path(__file__).parent / "persona-cyanmeow.txt"

# thinking 開啟時單句 20.3 秒不可用；關閉後暖機 1.05–1.64 秒（2026-07-17 實測）
LLAMA_EXTRA = {"chat_template_kwargs": {"enable_thinking": False}}

# 保底關鍵字表（規劃 §3.3 決策點 3）：命中即升級主 session，不給模型判斷
# 機會——任務型指令誤留在灰喵的代價（幻覺假執行）遠高於誤升級（只是變慢）。
ESCALATE_KEYWORDS = (
    "dispatch", "部署", "重啟", "restart", "commit", "push", "rollback", "回滾",
    "排程", "cron", "上線", "發布", "記憶", "快照", "備份", "測試", "build",
    "執行", "安裝", "更新", "刪除", "檔案", "報告", "整理",
)

# 升級判斷的輸出契約，跑在 persona 之後——persona 檔維持 Kevin 定稿原文，
# 工程指令不混進去
DECISION_SUFFIX = """
輸出規則：你必須輸出一個 JSON 物件 {"action": "...", "text": "..."}。
如果這句話是問候、閒聊、想法討論、情緒分享、請你給建議這類純對話，action 填 "reply"，text 填你要對 Kevin 說的話，照上面說話方式的規則。
如果這句話需要查資料、看即時狀態、操作系統、執行任務、讀寫記憶或檔案、排程、部署、開發相關動作，action 填 "escalate"，text 填空字串，這句話會轉給主系統處理。
拿不準的時候一律選 escalate。
"""

DECISION_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "voice_decision",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["reply", "escalate"]},
                "text": {"type": "string"},
            },
            "required": ["action", "text"],
            "additionalProperties": False,
        },
    },
}


def load_persona(path: Path = PERSONA_FILE) -> str:
    """讀 persona system prompt。檔案缺失時 fail fast——沒有 persona 的裸
    Qwen 會自稱通義千問，寧可服務起不來也不能帶錯身分上線。"""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Persona file is empty: {path}")
    return text


class ConversationHistory:
    """記憶體內滾動對話視窗——不落地、不寫 TriClaw 記憶。

    規劃 §3.2：最近 10 輪、30 分鐘 TTL。涉及記憶／狀態／任務的內容一律
    升級主 session（P2），本地歷史只服務對話連貫性。
    """

    def __init__(self, max_turns: int = 10, ttl: float = 1800.0):
        self._max_turns = max_turns
        self._ttl = ttl
        self._entries: list[tuple[str, str, float]] = []  # (role, content, ts)

    def append_exchange(self, user_text: str, reply: str, now: float | None = None) -> None:
        """成功往返才進歷史——失敗的請求不該污染下一輪 context。"""
        now = time.time() if now is None else now
        self._entries.append(("user", user_text, now))
        self._entries.append(("assistant", reply, now))
        self._trim(now)

    def messages(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        self._trim(now)
        return [{"role": role, "content": content} for role, content, _ in self._entries]

    def _trim(self, now: float) -> None:
        self._entries = [e for e in self._entries if now - e[2] <= self._ttl]
        excess = len(self._entries) - self._max_turns * 2  # 一輪＝user＋assistant 兩則
        if excess > 0:
            self._entries = self._entries[excess:]


class FastVoiceEngine:
    """灰喵推理引擎：persona＋滾動歷史 → llama.cpp chat completions。"""

    def __init__(
        self,
        llama_url: str,
        persona: str,
        timeout: float = 8.0,
        # 0.5 而非 0.7：三輪聽測比對（2026-07-17），0.7 會抽樣出狀態幻覺
        # 與禁句，0.5 十句全過誠實測試且韻律較穩
        temperature: float = 0.5,
        max_tokens: int = 300,
    ):
        self._llama_url = llama_url
        self._persona = persona
        self._timeout = timeout
        self._temperature = temperature
        self._max_tokens = max_tokens
        self.history = ConversationHistory()
        self._counter = itertools.count(1)

    def next_message_id(self) -> str:
        """與 mcp-bridge 的 voice-{ms}-{n} 同形，來源可從前綴區分。"""
        return f"fastvoice-{int(time.time() * 1000)}-{next(self._counter)}"

    @staticmethod
    def keyword_escalation(text: str) -> str | None:
        """回傳命中的保底關鍵字（無命中回 None）。"""
        lower = text.lower()
        return next((kw for kw in ESCALATE_KEYWORDS if kw in lower), None)

    async def decide(self, client: httpx.AsyncClient, text: str) -> tuple[str, str | None]:
        """單句判斷＋回覆，單次 llama 呼叫。

        回傳 ("reply", 回覆文字) 或 ("escalate", None)。逾時或 HTTP 錯誤
        直接拋出——呼叫端把例外也當 escalate 處理（fallback）。
        """
        kw = self.keyword_escalation(text)
        if kw:
            logger.info("fast-voice escalate (keyword=%r): text=%r", kw, text[:40])
            return ("escalate", None)

        messages = [{"role": "system", "content": self._persona + "\n" + DECISION_SUFFIX}]
        messages += self.history.messages()
        messages.append({"role": "user", "content": text})

        t_start = time.time()
        resp = await client.post(
            self._llama_url,
            json={
                "model": "fast-voice",
                "messages": messages,
                "temperature": self._temperature,
                "max_tokens": self._max_tokens,
                "response_format": DECISION_SCHEMA,
                **LLAMA_EXTRA,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        decision = json.loads(resp.json()["choices"][0]["message"]["content"])
        elapsed = time.time() - t_start

        if decision["action"] == "escalate":
            logger.info("fast-voice escalate (model, %.2fs): text=%r", elapsed, text[:40])
            return ("escalate", None)

        # 換行會讓 TTS 韻律斷裂不協調（2026-07-17 Kevin 聽測回饋）；句子
        # 本身已帶標點，直接把換行收掉成連續段落
        reply = re.sub(r"\s*\n+\s*", "", decision["text"].strip())
        if not reply:
            raise ValueError("fast-voice: empty reply text from llama.cpp")
        logger.info(
            "fast-voice reply: %.2fs text=%r reply=%r", elapsed, text[:40], reply[:60],
        )
        self.history.append_exchange(text, reply)
        return ("reply", reply)


class EscalationAliases:
    """升級轉送的 message_id 別名表。

    mcp-bridge /inject 一律自鑄 message_id（server.ts:91 不收外部值），而
    PWA 輪詢的是 fast-voice 原始 id。轉送時記 bridge_id → 原始 id，主
    session 的 voice_reply callback 進來時換回原始 id，前端零改動。
    """

    def __init__(self, ttl: float = 300.0):
        self._ttl = ttl
        self._aliases: dict[str, tuple[str, float]] = {}

    def register(self, bridge_id: str, original_id: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._aliases = {
            k: v for k, v in self._aliases.items() if now - v[1] <= self._ttl
        }
        self._aliases[bridge_id] = (original_id, now)

    def resolve(self, message_id: str, now: float | None = None) -> str:
        """TTL 內回原始 id（可重複解析、不消耗），到期或無別名回原樣。

        可重複解析（peek 而非 pop）是防雙投的必要條件：同一 bridge callback 若
        被重送，兩次都須解析成同一穩定原始 id，回覆匣的撞 ID 守衛才擋得住重複
        入匣；若一次性 pop，第二次會退回 bridge id、以不同 store_id 再入匣而繞過
        守衛（F2）。到期別名於此懶清。"""
        now = time.time() if now is None else now
        hit = self._aliases.get(message_id)
        if hit is None:
            return message_id
        if now - hit[1] > self._ttl:
            del self._aliases[message_id]  # 到期懶清
            return message_id
        return hit[0]
