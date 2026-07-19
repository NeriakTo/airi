"""server.py 回覆匣接線測試（票 6-2）——ASGI 層驅動真實路由。

透過 httpx.ASGITransport 打實際 /voice/reply-callback 與 /voice/reply/{id}，
把 server._reply_box 換成 tmp 路徑的 ReplyBox、_check_pin 放行。驗過渡相容的
兩條對外行為：legacy GET 取件自動 ACK 至 played＋再取不重播；撞 ID 拒絕且不
重複發 Discord 保底（防雙投）。
"""

import asyncio

import httpx
import pytest

import server
from fast_voice import EscalationAliases
from reply_box import ReplyBox, ReplyState


def _asgi() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://t")


def _install_box(monkeypatch, tmp_path, **kw) -> ReplyBox:
    box = ReplyBox(tmp_path / "reply_box.db", **kw)
    monkeypatch.setattr(server, "_reply_box", box)
    monkeypatch.setattr(server, "_check_pin", lambda request: True)
    return box


def test_legacy_get_auto_acks_to_played_then_no_replay(monkeypatch, tmp_path):
    box = _install_box(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "DISCORD_WEBHOOK", "")  # 免背景 webhook task

    async def run():
        async with _asgi() as c:
            cb = await c.post("/voice/reply-callback", json={"text": "長任務結果", "message_id": "m1"})
            first = await c.get("/voice/reply/m1")
            second = await c.get("/voice/reply/m1")
            return cb, first, second

    cb, first, second = asyncio.run(run())
    assert cb.json()["ok"] is True
    assert first.json() == {"status": "ready", "text": "長任務結果"}
    assert second.json() == {"status": "pending"}  # 再取不重播
    # 取件後態為 played，資料仍留匣中（供補聽/票 6-4）
    assert box.get("m1")[1] is ReplyState.PLAYED


def test_get_pending_before_callback(monkeypatch, tmp_path):
    _install_box(monkeypatch, tmp_path)

    async def run():
        async with _asgi() as c:
            return await c.get("/voice/reply/never")

    r = asyncio.run(run())
    assert r.json() == {"status": "pending"}


def test_collision_rejected_and_no_double_discord(monkeypatch, tmp_path):
    box = _install_box(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "DISCORD_WEBHOOK", "hook-set")

    posts: list[str] = []

    async def fake_webhook(text, *args, **kwargs):
        posts.append(text)

    monkeypatch.setattr(server, "_discord_post_webhook", fake_webhook)

    async def run():
        async with _asgi() as c:
            first = await c.post("/voice/reply-callback", json={"text": "回覆一", "message_id": "dup"})
            second = await c.post("/voice/reply-callback", json={"text": "重複投遞", "message_id": "dup"})
            await asyncio.sleep(0)  # 讓已排程的 webhook task 執行
            return first, second

    first, second = asyncio.run(run())
    assert first.json()["ok"] is True
    assert second.json() == {"ok": False, "duplicate": True, "runtime": "claude-code"}
    # 不覆蓋既有內容
    assert box.get("dup")[0] == "回覆一"
    # Discord 保底只發一次（撞 ID 不重複發）
    assert posts == ["🫧 回覆一"]


def test_empty_reply_rejected(monkeypatch, tmp_path):
    _install_box(monkeypatch, tmp_path)

    async def run():
        async with _asgi() as c:
            return await c.post("/voice/reply-callback", json={"text": "   ", "message_id": "x"})

    r = asyncio.run(run())
    assert r.status_code == 400


def test_empty_message_id_rejected_fail_loud(monkeypatch, tmp_path):
    # F4：無 message_id 無法入匣取件，回 400 fail-loud，而非 200 靜默吞。
    box = _install_box(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "DISCORD_WEBHOOK", "")

    async def run():
        async with _asgi() as c:
            return await c.post("/voice/reply-callback", json={"text": "無 id 回覆", "message_id": ""})

    r = asyncio.run(run())
    assert r.status_code == 400
    assert box.count() == 0  # 確未入匣


def test_bridge_callback_resend_does_not_double_deliver(monkeypatch, tmp_path):
    # F2：同一 bridge callback 重送，peek 解析成同一 original id，撞 ID 擋下第二筆、
    # 也不重複發 Discord。舊一次性 pop 下第二次退回 bridge id、以不同 store_id 再
    # 入匣＝雙投，此測試會 FAIL。
    box = _install_box(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "DISCORD_WEBHOOK", "hook-set")
    aliases = EscalationAliases(ttl=300)
    aliases.register("bridge-1", "original-1")  # 真實時鐘，兩次解析同秒內皆在 TTL
    monkeypatch.setattr(server, "_escalation_aliases", aliases)

    posts: list[str] = []

    async def fake_webhook(text, *args, **kwargs):
        posts.append(text)

    monkeypatch.setattr(server, "_discord_post_webhook", fake_webhook)

    async def run():
        async with _asgi() as c:
            first = await c.post("/voice/reply-callback", json={"text": "回覆", "message_id": "bridge-1"})
            second = await c.post("/voice/reply-callback", json={"text": "重送", "message_id": "bridge-1"})
            await asyncio.sleep(0)
            return first, second

    first, second = asyncio.run(run())
    assert first.json()["ok"] is True
    assert second.json() == {"ok": False, "duplicate": True, "runtime": "claude-code"}
    assert box.count() == 1
    assert box.get("original-1") is not None  # 入匣鍵為穩定 original id
    assert box.get("bridge-1") is None  # 不因重送落一筆 bridge id
    assert posts == ["🫧 回覆"]  # Discord 保底只發一次


def test_reply_box_lazy_init_reads_env_not_home(monkeypatch, tmp_path):
    # F5：lazy 單例首次使用才建，且讀 MEOWVOICE_REPLY_BOX_DB——不落真實家目錄。
    monkeypatch.setattr(server, "_reply_box", None)
    target = tmp_path / "sub" / "rb.db"
    monkeypatch.setenv("MEOWVOICE_REPLY_BOX_DB", str(target))
    box = server._get_reply_box()
    assert box._path == target  # 建在注入路徑
    assert str(box._path).startswith(str(tmp_path))  # 不在 ~/.meowvoice
