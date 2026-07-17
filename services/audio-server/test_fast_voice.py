"""fast_voice.py 單元測試。

覆蓋：歷史視窗（輪數上限／TTL／失敗不入史）、persona 載入 fail fast、
generate 的 payload 契約（thinking 關閉、persona 在首位、歷史夾中間）。
llama.cpp 以 httpx.MockTransport 替身；不 mock 的整合驗證走部署後
E2E smoke（POST /voice/dispatch runtime=fast-voice）。
"""

import asyncio
import json

import httpx
import pytest

from fast_voice import ConversationHistory, FastVoiceEngine, load_persona


# --- ConversationHistory ---

def test_history_keeps_last_n_turns():
    h = ConversationHistory(max_turns=2, ttl=9999)
    for i in range(5):
        h.append_exchange(f"q{i}", f"a{i}", now=100.0 + i)
    msgs = h.messages(now=110.0)
    assert len(msgs) == 4  # 2 輪 × 2 則
    assert msgs[0] == {"role": "user", "content": "q3"}
    assert msgs[-1] == {"role": "assistant", "content": "a4"}


def test_history_ttl_expiry():
    h = ConversationHistory(max_turns=10, ttl=1800)
    h.append_exchange("old-q", "old-a", now=0.0)
    h.append_exchange("new-q", "new-a", now=1000.0)
    msgs = h.messages(now=1900.0)  # old @0 已逾 1800s，new @1000 還在
    assert [m["content"] for m in msgs] == ["new-q", "new-a"]


def test_history_empty_when_all_expired():
    h = ConversationHistory(max_turns=10, ttl=60)
    h.append_exchange("q", "a", now=0.0)
    assert h.messages(now=61.1) == []


# --- load_persona ---

def test_load_persona_missing_file_fails(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_persona(tmp_path / "nope.txt")


def test_load_persona_empty_file_fails(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_persona(f)


def test_load_persona_real_file_has_identity_override():
    persona = load_persona()
    assert "青喵" in persona
    assert "通義千問" in persona  # 身分覆蓋條款必須在


# --- FastVoiceEngine.generate ---

def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _completion(text: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_generate_payload_contract():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _completion("好的 Kevin。")

    engine = FastVoiceEngine("http://test/v1/chat/completions", "PERSONA", timeout=8)
    reply = asyncio.run(engine.generate(_mock_client(handler), "你好"))

    assert reply == "好的 Kevin。"
    assert seen["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["messages"][0] == {"role": "system", "content": "PERSONA"}
    assert seen["messages"][-1] == {"role": "user", "content": "你好"}


def test_generate_threads_history_between_calls():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["messages"])
        return _completion(f"回覆{len(calls)}")

    engine = FastVoiceEngine("http://test/", "P")
    client = _mock_client(handler)
    asyncio.run(engine.generate(client, "第一句"))
    asyncio.run(engine.generate(client, "第二句"))

    # 第二次呼叫要帶第一輪完整往返：system, user1, assistant1, user2
    assert [m["role"] for m in calls[1]] == ["system", "user", "assistant", "user"]
    assert calls[1][1]["content"] == "第一句"
    assert calls[1][2]["content"] == "回覆1"


def test_generate_strips_newlines_for_tts_prosody():
    # 聽測回饋（2026-07-17）：逐行短句讓 TTS 韻律斷裂，回覆必須是連續段落
    def handler(request: httpx.Request) -> httpx.Response:
        return _completion("第一句。\n第二句。\n\n  第三句。")

    engine = FastVoiceEngine("http://test/", "P")
    reply = asyncio.run(engine.generate(_mock_client(handler), "hi"))
    assert reply == "第一句。第二句。第三句。"


def test_generate_failure_leaves_history_clean():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    engine = FastVoiceEngine("http://test/", "P")
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(engine.generate(_mock_client(handler), "會失敗的話"))
    assert engine.history.messages() == []


def test_generate_empty_completion_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return _completion("   ")

    engine = FastVoiceEngine("http://test/", "P")
    with pytest.raises(ValueError):
        asyncio.run(engine.generate(_mock_client(handler), "hi"))
    assert engine.history.messages() == []


def test_message_id_prefix_and_uniqueness():
    engine = FastVoiceEngine("http://test/", "P")
    ids = {engine.next_message_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(i.startswith("fastvoice-") for i in ids)
