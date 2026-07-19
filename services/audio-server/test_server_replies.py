"""server.py 未讀補取端點測試（票 6-4）——ASGI 層驅動真實路由。

覆蓋 GET /voice/replies（倒序＋20 筆上限＋摘要欄位＋state 名＋pin）與
POST /voice/reply/{id}/ack（合法遷移／冪等重複／亂序不降級／非法值 400／不存在
404／pin 未帶 401），以及模擬 PWA 補取呼叫序列的補取 E2E（積壓 3 筆→列表全列
→逐筆 read ACK 落匣→播放 audio 端點→played ACK 落匣→重開已 played 不入未讀集、
仍可手動點播）。

時鐘不涉入（列表／ACK 用真實時鐘、窗內全部存活）；合成以 cache.put 預置快取檔
取代（audio 命中回檔，不打真引擎——引擎延遲另由 6-3 量測腳本負責）。
"""

import asyncio
import time

import httpx

import server
from reply_box import ReplyBox, ReplyState
from reply_cache import ReplyCache

# 端點不吃 now 參數，內部走真實時鐘做滾動窗清理。故入匣須用貼近真實時鐘的
# 時間戳（否則 now=NOW＝1970 epoch 會被端點的 24h 到期清理立刻清掉）。
# 用 base+偏移確保順序可控又落在窗內。
NOW = time.time()


def _asgi() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://t")


def _install(monkeypatch, tmp_path, *, pin_ok=True, **box_kw):
    box = ReplyBox(tmp_path / "box.db", **box_kw)
    cache = ReplyCache(tmp_path / "cache")
    monkeypatch.setattr(server, "_reply_box", box)
    monkeypatch.setattr(server, "_reply_cache", cache)
    monkeypatch.setattr(server, "_check_pin", lambda request: pin_ok)
    monkeypatch.setattr(server, "DISCORD_WEBHOOK", "")
    return box, cache


# --- GET /voice/replies：倒序、上限、摘要、state 名、pin ---


def test_list_replies_time_descending_with_fields(monkeypatch, tmp_path):
    box, _ = _install(monkeypatch, tmp_path)
    box.enqueue("a", "先", now=NOW)
    box.enqueue("b", "中", now=NOW + 1)
    box.enqueue("c", "後", now=NOW + 2)
    box.ack("b", ReplyState.READ, now=NOW + 3)

    async def run():
        async with _asgi() as c:
            return await c.get("/voice/replies")

    r = asyncio.run(run())
    assert r.status_code == 200
    replies = r.json()["replies"]
    assert [x["id"] for x in replies] == ["c", "b", "a"]  # created_at 倒序
    # 欄位齊備：id、summary（文字摘要）、state（字串名）、created_at
    assert replies[0] == {"id": "c", "summary": "後", "state": "delivered", "created_at": NOW + 2}
    assert replies[1]["state"] == "read"  # 反映當前 ACK 態


def test_list_replies_caps_at_twenty(monkeypatch, tmp_path):
    box, _ = _install(monkeypatch, tmp_path)  # 預設 max_entries=20
    for i in range(25):
        box.enqueue(f"r{i:02d}", f"文字{i}", now=NOW + i)

    async def run():
        async with _asgi() as c:
            return await c.get("/voice/replies")

    r = asyncio.run(run())
    replies = r.json()["replies"]
    assert len(replies) == 20  # 一頁上限 20（與滾動窗同界）
    assert replies[0]["id"] == "r24"  # 最新在最前


def test_list_replies_summary_truncated_for_long_text(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "REPLY_SUMMARY_MAX_CHARS", 10)
    box, _ = _install(monkeypatch, tmp_path)
    box.enqueue("a", "零一二三四五六七八九十", now=NOW)  # 11 字 > 10

    async def run():
        async with _asgi() as c:
            return await c.get("/voice/replies")

    r = asyncio.run(run())
    summary = r.json()["replies"][0]["summary"]
    assert summary == "零一二三四五六七八九…"  # 截到上限並補刪節號


def test_list_replies_pin_required(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, pin_ok=False)

    async def run():
        async with _asgi() as c:
            return await c.get("/voice/replies")

    r = asyncio.run(run())
    assert r.status_code == 401


# --- GET /voice/reply/{id}/text：唯讀取文不改態（N1，live 路徑用）---


def test_reply_text_returns_full_text_without_changing_state(monkeypatch, tmp_path):
    # N1：live 路徑改用唯讀 /text 取文渲染——回全文且絕不改 ACK 態（仍 delivered），
    # 播放與 played ACK 另走 audio＋顯式 ack，故合成／播放失敗可重播。
    box, _ = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "很長的一段回覆全文", now=NOW)

    async def run():
        async with _asgi() as c:
            first = await c.get("/voice/reply/m1/text")
            second = await c.get("/voice/reply/m1/text")  # 重取仍 ready（非 legacy 的取件即消）
            return first, second

    first, second = asyncio.run(run())
    assert first.json() == {"status": "ready", "text": "很長的一段回覆全文"}
    assert second.json()["status"] == "ready"  # 唯讀：不像 legacy 取一次即 played
    assert box.get("m1")[1] is ReplyState.DELIVERED  # 態未被 /text 改動


def test_reply_text_not_found_returns_404(monkeypatch, tmp_path):
    # O4：改前此測試斷言 200 {"status":"pending"}，把錯誤契約固定了。裁決修正——
    # 入匣時 text 必與 reply 同筆寫入（enqueue(reply_id, text)），無「匣中存在但文字
    # 未備妥」場景，故 pending 無真實對應：不存在（含 live 尚未入匣）或已被滾動窗
    # 物理刪除，一律 404。live 輪詢據此續詢至入匣回 200 或超時。
    _install(monkeypatch, tmp_path)

    async def run():
        async with _asgi() as c:
            return await c.get("/voice/reply/never/text")

    r = asyncio.run(run())
    assert r.status_code == 404


def test_reply_text_pin_required(monkeypatch, tmp_path):
    box, _ = _install(monkeypatch, tmp_path, pin_ok=False)
    box.enqueue("m1", "回覆", now=NOW)

    async def run():
        async with _asgi() as c:
            return await c.get("/voice/reply/m1/text")

    r = asyncio.run(run())
    assert r.status_code == 401


def test_reply_text_over_window_deleted_returns_404(monkeypatch, tmp_path):
    # Q4：超窗物理刪除的 ID 打 /text 回 404（改前只有註解宣稱，無實測）。入匣 25 筆
    # （max_entries=20），最舊 5 筆（r00-r04）被滾動窗物理砍除；對已刪 r00 打 /text
    # 應 404——與「不存在」同語意，證明超窗刪除路徑確實回 404 而非復活。
    box, _ = _install(monkeypatch, tmp_path)  # 預設 max_entries=20
    for i in range(25):
        box.enqueue(f"r{i:02d}", f"文字{i}", now=NOW + i)
    assert box.get("r00") is None  # 前置：確認 r00 已被滾動窗物理刪除
    assert box.get("r24") is not None  # 最新仍在

    async def run():
        async with _asgi() as c:
            deleted = await c.get("/voice/reply/r00/text")  # 已刪
            alive = await c.get("/voice/reply/r24/text")    # 仍在
            return deleted, alive

    deleted, alive = asyncio.run(run())
    assert deleted.status_code == 404             # 超窗刪除＝404
    assert alive.json() == {"status": "ready", "text": "文字24"}


# --- POST /voice/reply/{id}/ack：遷移、冪等、亂序、非法、不存在、pin ---


def test_ack_read_then_played_advances(monkeypatch, tmp_path):
    box, _ = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "回覆", now=NOW)

    async def run():
        async with _asgi() as c:
            read = await c.post("/voice/reply/m1/ack", json={"state": "read"})
            played = await c.post("/voice/reply/m1/ack", json={"state": "played"})
            return read, played

    read, played = asyncio.run(run())
    assert read.json() == {"ok": True, "state": "read", "outcome": "advanced"}
    assert played.json() == {"ok": True, "state": "played", "outcome": "advanced"}
    assert box.get("m1")[1] is ReplyState.PLAYED  # 落匣至 played


def test_ack_duplicate_is_idempotent(monkeypatch, tmp_path):
    box, _ = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "回覆", now=NOW)

    async def run():
        async with _asgi() as c:
            first = await c.post("/voice/reply/m1/ack", json={"state": "read"})
            second = await c.post("/voice/reply/m1/ack", json={"state": "read"})
            return first, second

    first, second = asyncio.run(run())
    assert first.json()["outcome"] == "advanced"
    assert second.json()["outcome"] == "unchanged"  # 重複＝no-op 回成功
    assert second.status_code == 200


def test_ack_out_of_order_does_not_regress(monkeypatch, tmp_path):
    # 守門：played 後收到亂序低階 read ACK，高階態不得被覆蓋（不降級）。
    box, _ = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "回覆", now=NOW)

    async def run():
        async with _asgi() as c:
            await c.post("/voice/reply/m1/ack", json={"state": "played"})
            late_read = await c.post("/voice/reply/m1/ack", json={"state": "read"})
            return late_read

    late_read = asyncio.run(run())
    assert late_read.status_code == 200
    assert late_read.json()["outcome"] == "unchanged"  # 亂序低階不降級
    assert box.get("m1")[1] is ReplyState.PLAYED


def test_ack_invalid_state_400(monkeypatch, tmp_path):
    box, _ = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "回覆", now=NOW)

    async def run():
        async with _asgi() as c:
            # delivered 是入匣初態、非 client 動作 → 一併 400；亂打字串亦 400。
            bad = await c.post("/voice/reply/m1/ack", json={"state": "delivered"})
            junk = await c.post("/voice/reply/m1/ack", json={"state": "wat"})
            return bad, junk

    bad, junk = asyncio.run(run())
    assert bad.status_code == 400
    assert junk.status_code == 400
    assert box.get("m1")[1] is ReplyState.DELIVERED  # 非法值不動狀態


def test_ack_malformed_body_422(monkeypatch, tmp_path):
    # N5：契約分兩層——結構非法（缺 state／state:null）由 Pydantic 擋成 422
    # （FastAPI 慣例）；值非法（非 read／played 字串）由端點擋成 400（見上一測試）。
    box, _ = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "回覆", now=NOW)

    async def run():
        async with _asgi() as c:
            missing = await c.post("/voice/reply/m1/ack", json={})          # 缺 state 欄位
            null_state = await c.post("/voice/reply/m1/ack", json={"state": None})  # state:null
            return missing, null_state

    missing, null_state = asyncio.run(run())
    assert missing.status_code == 422  # 結構非法＝Pydantic 422
    assert null_state.status_code == 422
    assert box.get("m1")[1] is ReplyState.DELIVERED  # 結構非法不動狀態


def test_ack_unknown_id_404(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    async def run():
        async with _asgi() as c:
            return await c.post("/voice/reply/ghost/ack", json={"state": "read"})

    r = asyncio.run(run())
    assert r.status_code == 404


def test_ack_pin_required(monkeypatch, tmp_path):
    box, _ = _install(monkeypatch, tmp_path, pin_ok=False)
    box.enqueue("m1", "回覆", now=NOW)

    async def run():
        async with _asgi() as c:
            return await c.post("/voice/reply/m1/ack", json={"state": "read"})

    r = asyncio.run(run())
    assert r.status_code == 401
    assert box.get("m1")[1] is ReplyState.DELIVERED  # pin 拒絕不動狀態


def test_played_retry_sequence_list_compare_then_ack(monkeypatch, tmp_path):
    """played 重試序列的 server 端可測部分（O5⑤／Q3 誠實化）。

    範圍界定：本測試只驗 server 端不變量——「補取列表回非 played 態 → 重發 played
    ACK 冪等單調落匣 → 再列表回 played」。這是 PWA『UI 已播但匣內非 played 時據列表
    比對重發』流程中，落在 server 側、可用 HTTP 序列證明的一段。

    不在此測試範圍（屬 JS 靜態自查，非本測試宣稱）：client『首次 played ACK 因網路／
    pin 失敗而未送達』的製造，與 per-ID 重送鎖防併發——見 test-page.html retryPlayedAck
    的 playedRetryInflight 鎖（Q1）與 onended 的 rec.done 短路（Q2）。故測試命名不含
    『after_failure』，避免宣稱驗了未實測的 client 失敗路徑。

    起始態設為 read（等同 client 已回報 read、但 played ACK 尚未落匣的匣內狀態），
    直接驗列表比對後重發 played 能落匣。"""
    box, _ = _install(monkeypatch, tmp_path)
    box.enqueue("m1", "回覆", now=NOW)
    box.ack("m1", ReplyState.READ, now=NOW + 1)  # 匣內 read：played 尚未落匣的等價起始態

    async def run():
        async with _asgi() as c:
            before = await c.get("/voice/replies")            # 補取比對：m1 仍非 played
            retry = await c.post("/voice/reply/m1/ack", json={"state": "played"})  # 重發 played
            after = await c.get("/voice/replies")             # 重發後已 played
            return before, retry, after

    before, retry, after = asyncio.run(run())
    before_state = next(x["state"] for x in before.json()["replies"] if x["id"] == "m1")
    assert before_state == "read"                              # 列表回非 played＝觸發重發的判準
    assert retry.json()["outcome"] == "advanced"              # 重發落匣（冪等單調前進）
    assert box.get("m1")[1] is ReplyState.PLAYED
    after_state = next(x["state"] for x in after.json()["replies"] if x["id"] == "m1")
    assert after_state == "played"                            # 重發後 server 態 played


# --- 補取 E2E：模擬 PWA 重開補取呼叫序列 ---


def test_backfill_e2e_three_pending_read_play_reopen(monkeypatch, tmp_path):
    """積壓 3 筆 → 列表全列且順序正確 → 逐筆 read ACK 落匣（直查 box state）
    → 播放（audio 端點命中預置快取）→ played ACK 落匣 → 重開（重新列表）已 played
    項不在未讀集、手動點播仍可。"""
    box, cache = _install(monkeypatch, tmp_path)
    # 積壓 3 筆（鎖屏期間完成的長任務回覆，未經 live 輪詢，皆 delivered）。
    box.enqueue("m1", "回覆一", now=NOW)
    box.enqueue("m2", "回覆二", now=NOW + 1)
    box.enqueue("m3", "回覆三", now=NOW + 2)
    # 預置音訊快取，audio 端點命中回檔（不打真引擎；端點本身不改 ACK 態）。
    for rid in ("m1", "m2", "m3"):
        cache.put(rid, b"WAV:" + rid.encode())

    replies_holder: dict[str, list] = {}

    async def _fetch_list(c):
        r = await c.get("/voice/replies")
        return r.json()["replies"]

    async def run():
        async with _asgi() as c:
            # 1) 重開補取：列表全列、時間倒序（新→舊）。
            listing = await _fetch_list(c)
            replies_holder["first"] = listing
            # 2) 逐筆入視窗即 read ACK（PWA 對每筆呼叫一次）。
            for item in listing:
                await c.post("/voice/reply/" + item["id"] + "/ack", json={"state": "read"})
            # 3) 手動點播 m2：先取 audio（命中快取），播放完成才 played ACK。
            audio = await c.get("/voice/reply/m2/audio")
            await c.post("/voice/reply/m2/ack", json={"state": "played"})
            # 4) 重開＝重新列表。
            reopened = await _fetch_list(c)
            replies_holder["reopened"] = reopened
            # 5) 重開後手動點播已 played 的 m2 仍可（audio 端點不擋 played）。
            replay = await c.get("/voice/reply/m2/audio")
            return audio, replay

    audio, replay = asyncio.run(run())

    first = replies_holder["first"]
    assert [x["id"] for x in first] == ["m3", "m2", "m1"]  # 列表全列＋順序正確
    # 逐筆 read ACK 落匣：三筆皆前進至 read（直查 box state）。
    assert box.get("m1")[1] is ReplyState.READ
    assert box.get("m3")[1] is ReplyState.READ
    # 播放 m2：audio 端點命中快取回檔（200）＋播放完成的 played ACK 落匣。
    assert audio.status_code == 200
    assert audio.content == b"WAV:m2"
    assert box.get("m2")[1] is ReplyState.PLAYED
    # 重開：m2 已 played 不在未讀集；m1／m3 仍為未讀（read 非 played）。
    reopened = replies_holder["reopened"]
    m2_state = next(x["state"] for x in reopened if x["id"] == "m2")
    assert m2_state == "played"
    assert "m2" not in {x["id"] for x in reopened if x["state"] != "played"}
    assert {"m1", "m3"} <= {x["id"] for x in reopened if x["state"] != "played"}
    # 已 played 的 m2 手動點播仍可（audio 端點不因 played 擋播，可重播）。
    assert replay.status_code == 200
    assert replay.content == b"WAV:m2"
    # 重開後未讀集恰為 {m1, m3}（m2 已排除）。
    assert {x["id"] for x in reopened if x["state"] != "played"} == {"m1", "m3"}
