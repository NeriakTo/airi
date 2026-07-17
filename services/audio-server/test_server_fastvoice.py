"""server.py fast-voice 接線測試——灰喵故障 fallback 分支。

升級迴路的真實驗證走 E2E（2026-07-17 已過：keyword 升級 → bridge 注入 →
voice_reply → 別名解析 → 原 id 輪詢）。這裡只驗 E2E 難以觸發的分支：
decide 拋例外時必須走 escalate，而不是回錯誤文字。
"""

import asyncio

import httpx
import pytest

import server


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_decide_failure_falls_back_to_escalate(monkeypatch):
    async def broken_decide(client, text):
        raise httpx.ConnectError("llama down")

    escalated = {}

    async def fake_escalate(text, original_id):
        escalated["text"] = text
        escalated["original_id"] = original_id
        return True

    callbacks = []

    def handler(request: httpx.Request) -> httpx.Response:
        callbacks.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(server._fast_voice, "decide", broken_decide)
    monkeypatch.setattr(server, "_escalate_to_claude_code", fake_escalate)
    monkeypatch.setattr(
        server, "_http_client", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    asyncio.run(server._fast_voice_run("測試句", "http://cb/voice/reply-callback", "fastvoice-1-1"))

    assert escalated == {"text": "測試句", "original_id": "fastvoice-1-1"}
    assert callbacks == []  # 升級成功後不得再回 callback（回覆由主 session 出）


def test_decide_failure_and_escalate_failure_reports_honest_error(monkeypatch):
    async def broken_decide(client, text):
        raise httpx.ConnectError("llama down")

    async def failed_escalate(text, original_id):
        return False

    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(server._fast_voice, "decide", broken_decide)
    monkeypatch.setattr(server, "_escalate_to_claude_code", failed_escalate)
    monkeypatch.setattr(
        server, "_http_client", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    asyncio.run(server._fast_voice_run("測試句", "http://cb/voice/reply-callback", "fastvoice-1-2"))

    assert sent["message_id"] == "fastvoice-1-2"
    assert "沒有處理到" in sent["text"]  # 誠實故障提示，不是編造的回覆
