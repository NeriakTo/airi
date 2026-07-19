"""票 6-3 端到端量測（主控裁決 H4）：點播 miss vs hit 的真實延遲。

以 env 指向 tmp 的 reply_box／快取目錄、停用 Discord webhook，在 127.0.0.1:8402
起短命 uvicorn（真 CrispASR :8123、真 lifespan＝預合成 worker 起、真引擎合成），
入匣一筆→點播 miss 量測→點播 hit 量測→關閉。不碰 :8400、不動生產 reply_box／
快取資料；生產 CrispASR :8123 只打（本來就打）不重啟。

〔等價替代：monkeypatch _check_pin=True（繞 PIN，避免讀寫生產 pin 檔）與跳過
load_stt（STT 與 TTS 快取量測無關），其餘 lifespan（crispasr engine、worker
start、sweep）真跑。語意不變：量測走真 audio 端點端到端。〕

執行：.venv/bin/python services/audio-server/scripts/measure_reply_cache.py
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="meowvoice-measure-")
os.environ["MEOWVOICE_REPLY_BOX_DB"] = str(Path(_TMP) / "box.db")
os.environ["MEOWVOICE_REPLY_CACHE_DIR"] = str(Path(_TMP) / "cache")
os.environ["MEOWVOICE_DISCORD_WEBHOOK"] = ""
os.environ["MEOWVOICE_TTS_ENGINE"] = "crispasr"
os.environ["MEOWVOICE_PORT"] = "8402"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import server  # noqa: E402

server._check_pin = lambda request: True  # 繞 PIN，不碰生產 pin 檔


def _skip_stt() -> None:  # STT 與 TTS 快取量測無關，跳過以加速啟動
    return None


async def _skip_precache() -> None:
    # 啟動預熱 cached phrases 會持 _tts_lock 串行合成三句，與點播量測無關且會與
    # reply 合成搶同一把引擎鎖、拖過 3.5s 預算——量測時跳過。
    return None


server.load_stt = _skip_stt
server._precache_crispasr = _skip_precache

BASE = "http://127.0.0.1:8402"
# 端到端回完整 WAV，故量測用單句短回覆——CrispASR 長句 RTF 1.3-1.7，多句完整
# 合成會超過 3.5s 端到端預算（首音串流是另一議題，非本端點契約）。
TEXT = "青喵已經把事情處理完了。"
MID = "measure-e2e-1"


def _wait_ready(timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(BASE + "/voice/runtimes", timeout=1.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return False


def main() -> int:
    config = uvicorn.Config(server.app, host="127.0.0.1", port=8402, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        if not _wait_ready():
            print("server 未就緒（lifespan 未完成）", file=sys.stderr)
            return 1

        # 入匣一筆——同時觸發背景預合成 job。
        r = httpx.post(BASE + "/voice/reply-callback", json={"text": TEXT, "message_id": MID})
        r.raise_for_status()

        # miss：緊接點播，端點併入進行中的合成 job，端到端等其完成。
        t0 = time.perf_counter()
        miss = httpx.get(BASE + f"/voice/reply/{MID}/audio", timeout=10.0)
        miss_s = time.perf_counter() - t0
        miss.raise_for_status()

        # hit：快取已落檔，再次點播直接回檔。
        t0 = time.perf_counter()
        hit = httpx.get(BASE + f"/voice/reply/{MID}/audio", timeout=10.0)
        hit_s = time.perf_counter() - t0
        hit.raise_for_status()
    finally:
        srv.should_exit = True
        thread.join(timeout=10.0)

    miss_ok = "PASS" if miss_s < 3.5 else "FAIL"
    hit_ok = "PASS" if hit_s < 1.0 else "FAIL"
    print(f"cache-miss 點播（端到端合成）: {miss_s * 1000:.1f} ms  (門檻 <3500ms) [{miss_ok}]")
    print(f"命中快取點播（端到端）:       {hit_s * 1000:.1f} ms  (門檻 <1000ms) [{hit_ok}]")
    print(f"回檔 bytes: miss={len(miss.content)} hit={len(hit.content)}")
    return 0 if (miss_ok == "PASS" and hit_ok == "PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
