"""MeowVoice 本地音訊服務 — TTS + STT + Voice Bridge (E-lite V2)

TTS: Qwen3-TTS 1.7B MLX, STT: Breeze-ASR-25 (MediaTek 台灣華語微調)
Voice Bridge: 語音文字 → TriClaw runtime dispatch → TTS
"""

import io
import os
import json
import time
import wave
import asyncio
import hashlib
import secrets
import tempfile
import logging
import hmac
from collections import defaultdict
from contextlib import asynccontextmanager

from pathlib import Path

import mlx.core as mx
import numpy as np
import uvicorn
from fastapi import FastAPI, Request, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, FileResponse
import httpx

from dedup import DedupCache
from reply_box import ReplyBox
from reply_cache import ReplyCache, ReplyCacheFull
from fast_voice import EscalationAliases, FastVoiceEngine, load_persona
from crispasr_tts import CRISPASR_SAMPLE_RATE, CrispasrSynthError, CrispasrTtsEngine

logger = logging.getLogger("meowvoice-audio")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# dispatch 冪等快取（iOS 喚醒重放防護，見 dedup.py）
_dedup_cache = DedupCache()


def _load_dotenv():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


_load_dotenv()

TTS_MODEL_ID = os.environ.get(
    "MEOWVOICE_TTS_MODEL",
    "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
)
STT_MODEL_ID = os.environ.get(
    "MEOWVOICE_STT_MODEL",
    "eoleedi/Breeze-ASR-25-mlx",
)
TTS_VOICE = os.environ.get("MEOWVOICE_TTS_VOICE", "Chelsie")
HOST = os.environ.get("MEOWVOICE_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEOWVOICE_PORT", "8400"))

# --- TTS engine selection (ticket 6-1) ---
# Active TTS backend: "crispasr" (resident C++ ggml server, launchd
# dev.nerigate.meowvoice.crispasr) or "mlx" (in-process Qwen3-TTS). One-flip
# rollback lives in ~/.meowvoice/tts.json; MEOWVOICE_TTS_ENGINE overrides it
# (tests/CI). Default stays "mlx" so nothing switches until the flip is written.
# INVARIANT: the two engines never load together — in "crispasr" mode the MLX
# model is never loaded into this process (see lifespan / load_tts gating).
_TTS_ENGINE_CONFIG = Path(os.environ.get(
    "MEOWVOICE_TTS_CONFIG", str(Path.home() / ".meowvoice" / "tts.json")))


def _resolve_tts_engine() -> str:
    env = os.environ.get("MEOWVOICE_TTS_ENGINE", "").strip().lower()
    if env in ("crispasr", "mlx"):
        return env
    if _TTS_ENGINE_CONFIG.exists():
        try:
            engine = json.loads(_TTS_ENGINE_CONFIG.read_text()).get("engine", "").strip().lower()
            if engine in ("crispasr", "mlx"):
                return engine
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("TTS engine config unreadable (%s), defaulting to mlx", e)
    return "mlx"


TTS_ENGINE = _resolve_tts_engine()
CRISPASR_URL = os.environ.get(
    "MEOWVOICE_CRISPASR_URL", "http://127.0.0.1:8123/v1/audio/speech")
CRISPASR_VOICE = os.environ.get("MEOWVOICE_CRISPASR_VOICE", "serena")
CRISPASR_CONSENT = os.environ.get(
    "MEOWVOICE_CRISPASR_CONSENT", "Self-owned production voice reference (Kevin).")
# Failure-containment budgets (ticket 6-1 R1 / C1). Per-sentence timeout kept
# small so a deaf upstream cannot wedge the shared TTS lock; the whole-utterance
# deadline caps total lock-hold; the breaker fails new requests fast for its TTL.
CRISPASR_TIMEOUT = float(os.environ.get("MEOWVOICE_CRISPASR_TIMEOUT", "10"))
CRISPASR_RETRIES = int(os.environ.get("MEOWVOICE_CRISPASR_RETRIES", "1"))
CRISPASR_BACKOFF = float(os.environ.get("MEOWVOICE_CRISPASR_BACKOFF", "2"))
CRISPASR_DEADLINE = float(os.environ.get("MEOWVOICE_CRISPASR_DEADLINE", "30"))
CRISPASR_BREAKER_TTL = float(os.environ.get("MEOWVOICE_CRISPASR_BREAKER_TTL", "30"))

DISCORD_WEBHOOK = os.environ.get("MEOWVOICE_DISCORD_WEBHOOK", "")
VOICE_CHANNEL_ID = os.environ.get("MEOWVOICE_VOICE_CHANNEL_ID", "1475645959542145166")
CYANMEOW_BOT_ID = os.environ.get("MEOWVOICE_CYANMEOW_BOT_ID", "1490193787463532724")
DISPATCH_TIMEOUT = int(os.environ.get("MEOWVOICE_DISPATCH_TIMEOUT", "60"))
VOICE_PIN = os.environ.get("MEOWVOICE_PIN", "")
_BRIDGE_PIN = VOICE_PIN  # kept for localhost bridge auth after _init_pin_storage clears VOICE_PIN
VOICE_PLUGIN_URL = os.environ.get("MEOWVOICE_VOICE_PLUGIN", "http://127.0.0.1:8401")

# fast-voice adapter（灰喵本機 llama.cpp，見 fast_voice.py 模組說明）
FASTVOICE_LLAMA_URL = os.environ.get(
    "MEOWVOICE_FASTVOICE_LLAMA_URL", "http://127.0.0.1:8000/v1/chat/completions"
)
# 8s＝規劃 §4 的 fallback 門檻：超過就該讓 P2 轉 claude-code，不是繼續等
FASTVOICE_TIMEOUT = float(os.environ.get("MEOWVOICE_FASTVOICE_TIMEOUT", "8"))
_fast_voice = FastVoiceEngine(FASTVOICE_LLAMA_URL, load_persona(), timeout=FASTVOICE_TIMEOUT)
_escalation_aliases = EscalationAliases()

# --- PIN security infrastructure ---
_MEOWVOICE_DIR = Path.home() / ".meowvoice"
_PIN_HASH_FILE = _MEOWVOICE_DIR / "pin.hash"
_PIN_SALT_FILE = _MEOWVOICE_DIR / "pin.salt"
_RATE_LIMIT_MAX = 5
_RATE_LIMIT_WINDOW = 60
_SESSION_DURATION = 3600
_rate_limits: dict[str, list[float]] = defaultdict(list)
_sessions: dict[str, float] = {}


def _hash_pin(pin: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 100_000).hex()


def _init_pin_storage() -> None:
    global VOICE_PIN
    if _PIN_HASH_FILE.exists():
        VOICE_PIN = ""
        return
    if not VOICE_PIN:
        return
    _MEOWVOICE_DIR.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    _PIN_SALT_FILE.write_bytes(salt)
    os.chmod(str(_PIN_SALT_FILE), 0o600)
    _PIN_HASH_FILE.write_text(_hash_pin(VOICE_PIN, salt))
    os.chmod(str(_PIN_HASH_FILE), 0o600)
    VOICE_PIN = ""
    logger.info("PIN hash initialized (PBKDF2-SHA256, 100k rounds)")


def _verify_pin(pin: str) -> bool:
    if not pin:
        return False
    if not _PIN_HASH_FILE.exists() or not _PIN_SALT_FILE.exists():
        logger.warning("PIN hash files missing — run /voice/pin/setup from localhost")
        return False
    salt = _PIN_SALT_FILE.read_bytes()
    expected = _PIN_HASH_FILE.read_text().strip()
    return hmac.compare_digest(_hash_pin(pin, salt), expected)


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    attempts = _rate_limits[ip]
    _rate_limits[ip] = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    return len(_rate_limits[ip]) < _RATE_LIMIT_MAX


def _record_failed_attempt(ip: str) -> None:
    _rate_limits[ip].append(time.time())


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + _SESSION_DURATION
    return token


def _verify_session(token: str) -> bool:
    expiry = _sessions.get(token)
    if not expiry:
        return False
    if time.time() > expiry:
        del _sessions[token]
        return False
    return True

def _load_discord_bot_token() -> str:
    token = os.environ.get("MEOWVOICE_DISCORD_BOT_TOKEN", "")
    if token:
        return token
    token_file = os.environ.get("MEOWVOICE_DISCORD_BOT_TOKEN_FILE", "")
    if token_file and Path(token_file).exists():
        for line in Path(token_file).read_text().splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""

DISCORD_BOT_TOKEN = _load_discord_bot_token()

CHANNEL_ROUTES: list[tuple[list[str], str]] = [
    (["triclaw", "openclaw", "三爪"], "1486183810143097093"),
    (["連線", "linknet", "mes"], "1480803774116266110"),
    (["全有", "forecast"], "1489111296103288952"),
    (["印比雅", "鉅茂", "itec"], "1511980894246801538"),
    (["dg+", "dg plus"], "1504401236349157547"),
    (["三菱", "電梯", "facteye"], "1521075633441214566"),
    (["六哥"], "1519927940866117732"),
    (["紡織雲", "itextiles"], "1514862156401610752"),
    (["昕鈺", "bom"], "1485467155456589954"),
    (["科治"], "1488554316452073493"),
    (["鏈騏", "貴金屬"], "1508364794804047913"),
    (["車牌"], "1477308834128068741"),
    (["雲市集", "工業館"], "1478288192250843289"),
    (["鼎洰", "ems"], "1498531961797480469"),
    (["140d"], "1521093790197219478"),
    (["來永", "ai hr", "招聘"], "1475786438397132801"),
    (["帝寶", "物業"], "1489613734581374976"),
    (["章治", "rdt"], "1499551630914486283"),
    (["聯祥", "eqa"], "1484010621090402383"),
    (["鴻法", "sbir"], "1516963879702237245"),
    (["品牌", "blog", "nerigate"], "1475418652173008919"),
    (["家庭", "kelly", "feon"], "1520224473016566011"),
    (["gx10 建置", "gx10 規劃"], "1523300651092672622"),
    (["gx10", "a100", "azure"], "1505045689351147631"),
    (["fortigate", "70d"], "1520807144234942494"),
    (["呼嚕嚕", "purr"], "1504786668849201262"),
    (["威益"], "1487269649732341770"),
    (["富永"], "1475782950246420611"),
]

STT_BASE_PROMPT = "以下是繁體中文與英文混雜語音的轉錄。"
STT_GLOBAL_TERMS = (
    "青喵、黑喵、貓爪、灰喵、小野、TriClaw、MeowVoice、Kevin、"
    "Claude Code、Codex、Hermes、Anthropic、Discord、Tauri、Live2D、"
    "Breeze、Qwen、MLX、Electron"
)

CHANNEL_TERMS: dict[str, str] = {
    "1486183810143097093": "TriClaw、EventStore、correlation ID、SSE、Kernel、Runtime、Skill、dispatch、daemon",
    "1480803774116266110": "連線科技、MES、Galaxy、webhook、schema、Drizzle、BOM、HURCO、Dictionary、Pipeline",
    "1489111296103288952": "全有織造、forecast、MCO、品號、MES",
    "1511980894246801538": "印比雅、鉅茂、ITEC、BI、POC",
    "1504401236349157547": "DG+、MQTT、派車、dispatch",
    "1521075633441214566": "三菱電梯、Facteye、CEC、Node.js",
    "1514862156401610752": "紡織雲、iTEXTILES、QR、Galaxy",
    "1485467155456589954": "昕鈺、BOM、CAD、Galaxy",
    "1475786438397132801": "來永、AI HR、Supabase、104、webhook、Galaxy",
    "1489613734581374976": "帝寶、物業、Hermes、Galaxy",
    "1499551630914486283": "章治、RDT、SBIR、補助",
    "1484010621090402383": "聯祥、e-QA、品質管理",
    "1516963879702237245": "鴻法、SBIR、HPM",
    "1505045689351147631": "GX10、A100、Azure、GPU、DGX",
    "1523300651092672622": "FY-DGX01、GX10、Hermes、Andy",
    "1520807144234942494": "FortiGate、70D、SSL VPN、防火牆",
    "1487269649732341770": "威益、FortiGate、140D、Galaxy",
    "1475782950246420611": "富永、梁顧問、輔導、對帳單",
}


def build_stt_prompt(channel_id: str = "") -> str:
    parts = [STT_BASE_PROMPT]
    channel_terms = CHANNEL_TERMS.get(channel_id, "")
    all_terms = STT_GLOBAL_TERMS
    if channel_terms:
        all_terms += "、" + channel_terms
    parts.append(f"{all_terms}是專有名詞。")
    return "".join(parts)


GATEWAY_CONFIG_PATH = Path(os.environ.get(
    "MEOWVOICE_GATEWAY_CONFIG",
    str(Path.home() / ".meowvoice" / "gateway.json"),
))
_gateway_config: dict = {}


def _load_gateway_config() -> dict:
    global _gateway_config, CHANNEL_ROUTES, CHANNEL_TERMS, STT_GLOBAL_TERMS, VOICE_CHANNEL_ID
    if not GATEWAY_CONFIG_PATH.exists():
        logger.warning("Gateway config not found: %s, using defaults", GATEWAY_CONFIG_PATH)
        return {}
    try:
        with open(GATEWAY_CONFIG_PATH) as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Gateway config parse failed: %s, using defaults", e)
        return {}
    _gateway_config = config
    routing = config.get("channel_routing", {})
    try:
        routes = routing.get("routes", [])
        if routes:
            CHANNEL_ROUTES = [(r["prefixes"], r["channel_id"]) for r in routes]
    except (KeyError, TypeError) as e:
        logger.error("Gateway routes malformed: %s, keeping defaults", e)
    if routing.get("terminology"):
        CHANNEL_TERMS = routing["terminology"]
    if routing.get("global_terms"):
        STT_GLOBAL_TERMS = routing["global_terms"]
    if routing.get("default_channel_id"):
        VOICE_CHANNEL_ID = routing["default_channel_id"]
    rt_count = sum(1 for r in config.get("runtimes", {}).values() if r.get("enabled"))
    logger.info("Gateway config loaded: %d runtimes, %d routes", rt_count, len(CHANNEL_ROUTES))
    return config


_load_gateway_config()


tts_model = None
stt_model_id = None
_tts_lock = asyncio.Lock()
_stt_lock = asyncio.Lock()
_http_client: httpx.AsyncClient | None = None

# CrispASR adapter is created only in "crispasr" mode; stays None under MLX so
# the two backends never coexist in this process.
_crispasr: CrispasrTtsEngine | None = None
# Holds background startup tasks (precache) so they are not GC'd mid-flight.
_startup_tasks: set[asyncio.Task] = set()
# Cached CrispASR liveness for /health, refreshed at most every 5s (C1/H2) so a
# health-poll storm cannot hammer the upstream. ts is monotonic seconds.
_crispasr_health: dict[str, float | bool] = {"ts": 0.0, "ok": False}
# Serialises concurrent cold probes (single-flight, R3 H2): only one probe runs,
# the rest wait and read its fresh result — so no out-of-order stale overwrite.
_crispasr_probe_lock = asyncio.Lock()


def load_tts():
    global tts_model
    # Mutual-exclusion guard: in crispasr mode the MLX model must never load
    # into this process (memory + ticket 6-1 §5 invariant). Callers on the
    # crispasr path go through _crispasr instead.
    if TTS_ENGINE != "mlx":
        raise RuntimeError(f"load_tts() called under TTS_ENGINE={TTS_ENGINE}; MLX must not load")
    if tts_model is not None:
        return tts_model
    logger.info("Loading TTS model: %s", TTS_MODEL_ID)
    t = time.time()
    from mlx_audio.tts.utils import load_model
    tts_model = load_model(model_path=TTS_MODEL_ID)
    logger.info("TTS model loaded in %.1fs (sample_rate=%d)", time.time() - t, tts_model.sample_rate)
    return tts_model


def load_stt():
    global stt_model_id
    stt_model_id = STT_MODEL_ID
    logger.info("STT model configured: %s (lazy load on first request)", STT_MODEL_ID)
    return stt_model_id


CACHED_PHRASES: dict[str, str] = {
    "ack": "收到了，正在處理。",
    "heartbeat": "還在處理中，請稍候。",
    "timeout": "這個問題需要多一點時間，已排入背景處理。",
}
_voice_cache: dict[str, bytes] = {}


async def _precache_crispasr() -> None:
    """Pre-generate standard feedback phrases via the resident CrispASR server."""
    for key, phrase in CACHED_PHRASES.items():
        t0 = time.time()
        try:
            # J3：取 _tts_lock 逐句合成，與預合成 worker／點播序列化——暖機不與
            # reply 合成併發搶佔同一常駐引擎（實測併發合成數 >1 會拖慢首音）。
            async with _tts_lock:
                result = await _crispasr.synth(_http_client, phrase)
        except Exception as e:
            logger.warning("Failed to pre-cache voice '%s' via crispasr: %s", key, e)
            continue
        _voice_cache[key] = _crispasr.pcm_to_wav(result.pcm)
        logger.info("Cached voice '%s' (crispasr): %d bytes, %.1fs", key, len(_voice_cache[key]), time.time() - t0)


def _generate_cached_voices() -> None:
    """Pre-generate standard voice feedback phrases at startup (MLX path)."""
    model = load_tts()
    for key, phrase in CACHED_PHRASES.items():
        t0 = time.time()
        audio_chunks = []
        for result in model.generate(
            text=phrase, voice=TTS_VOICE, lang_code="chinese",
            verbose=False, stream=False,
        ):
            if hasattr(result, "audio"):
                audio_chunks.append(np.array(result.audio))
        if not audio_chunks:
            logger.warning("Failed to pre-cache voice: %s", key)
            continue
        full = np.concatenate(audio_chunks)
        audio_int16 = (full * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(model.sample_rate)
            wf.writeframes(audio_int16.tobytes())
        _voice_cache[key] = buf.getvalue()
        logger.info("Cached voice '%s': %d bytes, %.1fs", key, len(_voice_cache[key]), time.time() - t0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client, _crispasr, _reply_box_sweeper_task
    _http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    _init_pin_storage()
    # 回覆匣：啟動即清一次（涵蓋停機 >24h 後重啟），再掛週期清理，讓逾期敏感
    # 內容不因服務無流量而滯留磁碟（F3）。
    _get_reply_box().sweep()
    _reply_box_sweeper_task = asyncio.create_task(_reply_box_sweeper())
    # 孤兒快取清理無條件執行（G4）：CrispASR crash 後改以 MLX 回退重啟時，殘留的
    # .part 半成品／孤兒 .wav 也要清；不預合成不等於不清磁碟。start()（僅 crispasr
    # 模式）留在下方引擎分支。
    _get_reply_cache().sweep_orphans(_get_reply_box().active_ids())
    # Backend selection is mutually exclusive: only one TTS engine is warmed.
    if TTS_ENGINE == "crispasr":
        _crispasr = CrispasrTtsEngine(
            CRISPASR_URL, CRISPASR_VOICE, CRISPASR_CONSENT,
            timeout=CRISPASR_TIMEOUT, retries=CRISPASR_RETRIES, backoff=CRISPASR_BACKOFF,
            deadline=CRISPASR_DEADLINE, breaker_ttl=CRISPASR_BREAKER_TTL)
        logger.info("TTS engine=crispasr url=%s voice=%s (MLX not loaded)", CRISPASR_URL, CRISPASR_VOICE)
        # Precache is best-effort and OFF the readiness path: a slow/dead upstream
        # must not delay 'ready' (C1 ④). Runs in the background; failures are logged.
        _t = asyncio.create_task(_precache_crispasr())
        _startup_tasks.add(_t)
        _t.add_done_callback(_startup_tasks.discard)
        # CrispASR 模式：worker 啟動＋入匣預合成（precache=True）。
        _get_reply_cache().start(_synthesize_wav_bytes, precache=True)
    else:
        load_tts()
        _generate_cached_voices()
        # J1：MLX 模式也啟動 worker——點播 miss 一律走統一入口與唯一發布守衛；
        # precache=False＝維持「不預合成」（入匣不排 job）。
        _get_reply_cache().start(_synthesize_wav_bytes, precache=False)
    load_stt()
    voice_ok = bool(DISCORD_WEBHOOK and DISCORD_BOT_TOKEN)
    logger.info("Audio server ready on %s:%d (engine=%s, voice_bridge=%s, cached=%d)",
                HOST, PORT, TTS_ENGINE, voice_ok, len(_voice_cache))
    yield
    if _reply_box_sweeper_task is not None:
        _reply_box_sweeper_task.cancel()
    await _get_reply_cache().stop()  # 取消預合成 worker（未 start 為 no-op）
    await _http_client.aclose()
    logger.info("Audio server shutting down")


app = FastAPI(title="MeowVoice Audio Server", lifespan=lifespan)
ALLOWED_ORIGINS = os.environ.get(
    "MEOWVOICE_CORS_ORIGINS",
    "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,https://127.0.0.1:8400,https://asr.nerigate.dev,https://airi.nerigate.dev",
).split(",")
app.add_middleware(
    CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"], allow_headers=["*"], allow_credentials=True,
)

SSL_CERT = os.environ.get("MEOWVOICE_SSL_CERT", "/tmp/meowvoice-cert.pem")
SSL_KEY = os.environ.get("MEOWVOICE_SSL_KEY", "/tmp/meowvoice-key.pem")
TEST_PAGE = Path(__file__).parent / "test-page.html"


async def _probe_crispasr() -> bool:
    """Liveness of the resident CrispASR server, cached for 5s (H2).

    A GET :8123/health is cheap but /health can be polled hard, so the result is
    memoised for 5s. Any transport error or non-200 marks the upstream down.
    """
    now = time.monotonic()
    # 0.0 <= age < 5.0: a fresh cache short-circuits, but a "future" ts (age < 0)
    # is not treated as fresh — it must fall through so the monotonic guard runs.
    if 0.0 <= now - float(_crispasr_health["ts"]) < 5.0:
        return bool(_crispasr_health["ok"])
    # Single-flight (R3 H2): serialise concurrent cold probes so only one hits
    # the upstream; waiters re-check the now-fresh cache instead of racing.
    async with _crispasr_probe_lock:
        started = time.monotonic()
        if 0.0 <= started - float(_crispasr_health["ts"]) < 5.0:
            return bool(_crispasr_health["ok"])  # another waiter just refreshed it
        ok = False
        if _http_client is not None:
            base = CRISPASR_URL.split("/v1/", 1)[0]  # http://127.0.0.1:8123
            try:
                resp = await _http_client.get(f"{base}/health", timeout=2.0)
                ok = resp.status_code == 200
            except Exception:
                ok = False
        # Monotonic guard: an older probe must not overwrite a newer cached result.
        # If a newer result already won (started < stored ts), return THAT value so
        # every reader at this instant agrees on the latest result (R4 H2).
        if started >= float(_crispasr_health["ts"]):
            _crispasr_health.update(ts=started, ok=ok)
            return ok
        return bool(_crispasr_health["ok"])


@app.get("/health")
async def health():
    if TTS_ENGINE == "crispasr":
        # crispasr_ready reflects a live upstream probe, not just adapter presence:
        # an upstream 503 must surface as status=degraded (H2), not a false "ok".
        ready = _crispasr is not None and await _probe_crispasr()
        return {
            "status": "ok" if ready else "degraded",
            "tts_engine": TTS_ENGINE,
            "tts_model": CRISPASR_URL,
            # false here is the mutual-exclusion witness: no MLX weights in-process
            "tts_loaded": tts_model is not None,
            "crispasr_ready": ready,
            "stt_model": STT_MODEL_ID,
            "tts_sample_rate": CRISPASR_SAMPLE_RATE,
        }
    return {
        "status": "ok",
        "tts_engine": TTS_ENGINE,
        "tts_model": TTS_MODEL_ID,
        "tts_loaded": tts_model is not None,
        "crispasr_ready": None,
        "stt_model": STT_MODEL_ID,
        "tts_sample_rate": tts_model.sample_rate if tts_model else None,
    }


from pydantic import BaseModel

TTS_TEMPERATURE = float(os.environ.get("MEOWVOICE_TTS_TEMPERATURE", "0.5"))
TTS_SPEED = float(os.environ.get("MEOWVOICE_TTS_SPEED", "1.0"))
TTS_SEED = int(os.environ.get("MEOWVOICE_TTS_SEED", "42"))

class TtsRequest(BaseModel):
    text: str
    voice: str = TTS_VOICE
    lang: str = "chinese"
    temperature: float = TTS_TEMPERATURE
    speed: float = TTS_SPEED
    seed: int = TTS_SEED
    # stream 保留供 model.generate() 內部分段用，HTTP 回應一律為完整 WAV
    stream: bool = False

@app.post("/tts")
async def tts_generate(req: TtsRequest):
    """Generate speech from text. Returns WAV audio."""
    # Breaker check is BEFORE the lock (C1 ③): while the upstream is tripped,
    # new requests fail-fast with 503 instead of queuing behind the dead engine
    # and holding _tts_lock for the whole deadline.
    if TTS_ENGINE == "crispasr" and _crispasr is not None and _crispasr.is_unhealthy():
        return JSONResponse(
            {"error": "TTS engine unavailable", "detail": "circuit breaker open"},
            status_code=503,
        )
    async with _tts_lock:
        # Double-check under the lock (R3 C1): the breaker may have tripped while
        # this request was queued for _tts_lock. A queued request must not still
        # hit a dead upstream — release the lock and 503 instead.
        if TTS_ENGINE == "crispasr" and _crispasr is not None and _crispasr.is_unhealthy():
            return JSONResponse(
                {"error": "TTS engine unavailable", "detail": "circuit breaker open"},
                status_code=503,
            )
        return await _tts_generate_inner(req)


async def _tts_generate_inner(req: TtsRequest):
    if TTS_ENGINE == "crispasr":
        return await _crispasr_generate(req)
    # MLX 生成走共用核心（K3，與 reply worker 同一份、參數完整）；對外仍是完整
    # WAV／空音訊 500，行為不變。
    t_start = time.time()
    try:
        wav = _mlx_generate_wav(
            req.text,
            voice=req.voice,
            temperature=req.temperature,
            speed=req.speed,
            seed=req.seed,
            lang=req.lang,
            stream=req.stream,
        )
    except RuntimeError:
        return JSONResponse({"error": "No audio generated"}, status_code=500)
    logger.info("TTS done: text=%r bytes=%d time=%.2fs", req.text[:30], len(wav), time.time() - t_start)
    return StreamingResponse(io.BytesIO(wav), media_type="audio/wav")


async def _crispasr_generate(req: TtsRequest):
    """/tts via the resident CrispASR server with sentence-level segmentation.

    Delegates ordering + skip-and-continue recovery to CrispasrTtsEngine.synth
    (ticket 6-1 §2); a degraded (some sentences dropped) result still returns
    audio, only fully-failed synthesis becomes a 500.
    """
    t_start = time.time()
    try:
        # voice is fixed to the vendored serena clone; req.voice (an MLX speaker
        # name) does not map onto the CrispASR voice-dir, so it is ignored here.
        result = await _crispasr.synth(_http_client, req.text)
    except CrispasrSynthError as e:
        # Upstream dead / deadline / breaker => 503 (service unavailable), the
        # honest signal for a resident dependency being down.
        logger.warning("CrispASR TTS failed: text=%r err=%s", req.text[:30], e)
        return JSONResponse({"error": "TTS engine unavailable", "detail": str(e)}, status_code=503)

    if result.degraded:
        logger.warning("CrispASR TTS degraded: %d/%d sentence(s) dropped (idx=%s) text=%r",
                       len(result.failed), len(result.sentences), result.failed, req.text[:30])
    logger.info("TTS done [crispasr]: text=%r sentences=%d bytes=%d time=%.2fs",
                req.text[:30], len(result.sentences), len(result.pcm), time.time() - t_start)
    wav = _crispasr.pcm_to_wav(result.pcm)
    return StreamingResponse(io.BytesIO(wav), media_type="audio/wav")


@app.post("/stt")
async def stt_transcribe(
    request: Request,
    file: UploadFile = File(..., description="WAV audio file"),
    lang: str = Query(default="zh", description="Language hint"),
    channel_id: str = Query(default="", description="Channel ID for terminology prompt"),
):
    """Transcribe audio to text using mlx-whisper with dynamic terminology."""
    if not _check_pin(request):
        return JSONResponse({"error": "Invalid PIN"}, status_code=401)
    import mlx_whisper  # noqa: E402 — lazy import to avoid load at startup

    t_start = time.time()

    suffix = Path(file.filename).suffix if file.filename else ".wav"
    if not suffix:
        suffix = ".wav"
    content = await file.read()
    # iOS 麥克風 track 靜默失效時，前端會送出空的或缺檔頭的錄音檔——
    # 這是客戶端問題，回 400 讓前端引導重錄，不能落 500 traceback
    if len(content) < 100:
        logger.warning("STT rejected: audio too small (%d bytes, suffix=%s)", len(content), suffix)
        return JSONResponse({"error": "audio_decode_failed", "detail": "audio too small"}, status_code=400)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        prompt = build_stt_prompt(channel_id)
        try:
            async with _stt_lock:
                result = mlx_whisper.transcribe(
                    tmp_path,
                    path_or_hf_repo=stt_model_id,
                    language=lang,
                    verbose=False,
                    initial_prompt=prompt,
                    # Breeze-ASR-25 短語音場景 True 可提升連貫性；長錄音有幻覺傳播風險
                    condition_on_previous_text=True,
                )
        except RuntimeError as e:
            # mlx_whisper 對 ffmpeg 解不開的音檔拋 "Failed to load audio"；
            # 其他 RuntimeError 屬伺服器側問題，照舊往上拋成 500
            if "Failed to load audio" in str(e):
                logger.warning("STT audio decode failed: size=%d suffix=%s", len(content), suffix)
                return JSONResponse({"error": "audio_decode_failed", "detail": "undecodable audio"}, status_code=400)
            raise
        text = result.get("text", "").strip()
        duration = time.time() - t_start
        logger.info("STT done: text=%r time=%.2fs prompt_channel=%s", text[:50], duration, channel_id or "default")
        return {"text": text, "language": lang, "duration": duration}
    finally:
        os.unlink(tmp_path)


@app.get("/v1/models")
async def openai_models():
    """OpenAI-compatible model list for AIRI provider discovery."""
    return {
        "object": "list",
        "data": [
            {"id": "qwen3-tts", "object": "model", "owned_by": "meowvoice"},
            {"id": "breeze-asr-25", "object": "model", "owned_by": "meowvoice"},
            {"id": "triclaw-dispatch", "object": "model", "owned_by": "meowvoice"},
        ],
    }


class OpenAISpeechRequest(BaseModel):
    model: str = "qwen3-tts"
    input: str
    voice: str = TTS_VOICE
    response_format: str = "wav"
    speed: float = 1.0


@app.post("/v1/audio/speech")
async def openai_speech(req: OpenAISpeechRequest):
    """OpenAI-compatible TTS endpoint for AIRI xsai integration."""
    native_req = TtsRequest(text=req.input, voice=req.voice, lang="zh", stream=False)
    return await tts_generate(native_req)


@app.post("/v1/audio/transcriptions")
async def openai_transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str = Query(default="breeze-asr-25"),
    language: str = Query(default="zh"),
    channel_id: str = Query(default="", alias="channel_id"),
):
    """OpenAI-compatible STT endpoint with dynamic terminology."""
    if not _check_pin(request):
        return JSONResponse({"error": "Invalid PIN"}, status_code=401)
    result = await stt_transcribe(request=request, file=file, lang=language, channel_id=channel_id)
    if isinstance(result, JSONResponse):
        return result
    return {"text": result["text"]}


class ChatCompletionRequest(BaseModel):
    model: str = "triclaw-dispatch"
    messages: list[dict]
    temperature: float = 0.7
    max_tokens: int | None = None
    stream: bool = False


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, req: ChatCompletionRequest):
    """OpenAI-compatible chat endpoint — injects into Claude Code session via voice plugin."""
    if not _check_pin(request):
        return JSONResponse({"error": "Invalid PIN"}, status_code=401)
    user_messages = [m["content"] for m in req.messages if m.get("role") == "user"]
    if not user_messages:
        return JSONResponse({"error": "No user message"}, status_code=400)

    prompt_text = user_messages[-1]
    result = await _dispatch_to_runtime(prompt_text)

    if "error" in result:
        return JSONResponse(
            {"error": {"message": f"Voice inject failed: {result['error']}", "type": "server_error"}},
            status_code=502,
        )

    reply = f"[Injected into Claude Code session: {result.get('message_id', '')}]"

    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "model": req.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _route_prefix(text: str) -> tuple[str, str]:
    """Parse prefix from text and return (routed_channel_id, cleaned_text)."""
    lower = text.lower().strip()
    for prefixes, channel_id in CHANNEL_ROUTES:
        for prefix in prefixes:
            if lower.startswith(prefix):
                rest = text.strip()[len(prefix):].strip().lstrip("，,、：:").strip()
                if rest:
                    return channel_id, rest
                return channel_id, text.strip()
    return VOICE_CHANNEL_ID, text.strip()


async def _discord_post_webhook(text: str, username: str = "Kevin (語音)") -> dict | None:
    """Post a message via Discord webhook."""
    if not DISCORD_WEBHOOK or not _http_client:
        return None
    try:
        resp = await _http_client.post(
            DISCORD_WEBHOOK, params={"wait": "true"},
            json={"content": text, "username": username},
        )
        return resp.json() if resp.is_success else None
    except Exception as e:
        logger.error("Webhook post failed: %s", e)
        return None


class VoiceDispatchRequest(BaseModel):
    text: str
    channel_hint: str = ""
    runtime: str | None = None
    client_msg_id: str = ""


def _check_pin(request: Request) -> bool:
    """Validate via session cookie first, then PIN header with rate limiting."""
    session_token = request.cookies.get("meowvoice_session")
    if session_token and _verify_session(session_token):
        return True
    pin = request.headers.get("x-voice-pin", "")
    if not pin:
        return False
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        return False
    if _verify_pin(pin):
        return True
    _record_failed_attempt(client_ip)
    return False


async def _dispatch_to_runtime(text: str, runtime_id: str | None = None) -> dict:
    """Dispatch voice text to the specified (or default) runtime adapter."""
    if not _http_client:
        return {"error": "HTTP client not initialized"}
    runtimes = _gateway_config.get("runtimes", {})
    if not runtimes:
        return await _inject_legacy(text)
    if runtime_id:
        runtime = runtimes.get(runtime_id)
        if not runtime:
            return {"error": f"Unknown runtime: {runtime_id}"}
        if not runtime.get("enabled"):
            return {"error": f"Runtime disabled: {runtime_id}"}
    else:
        runtime_id, runtime = next(
            ((rid, r) for rid, r in runtimes.items() if r.get("default") and r.get("enabled")),
            (None, None),
        )
        if not runtime:
            return await _inject_legacy(text)
    callback_url = _gateway_config.get("callback_url", f"http://127.0.0.1:{PORT}/voice/reply-callback")
    try:
        resp = await _http_client.post(
            runtime["url"],
            json={"text": text, "callback_url": callback_url, "user": "Kevin"},
            headers={"X-Voice-Pin": _BRIDGE_PIN},
        )
        result = resp.json()
        result["runtime"] = runtime_id
        return result
    except Exception as e:
        logger.error("Runtime dispatch failed [%s]: %s", runtime_id, e)
        return {"error": str(e)}


async def _inject_legacy(text: str) -> dict:
    """Fallback: direct injection to MCP plugin when no gateway config."""
    if not _http_client:
        return {"error": "HTTP client not initialized"}
    try:
        resp = await _http_client.post(
            f"{VOICE_PLUGIN_URL}/inject",
            json={"text": text},
            headers={"X-Voice-Pin": _BRIDGE_PIN},
        )
        return resp.json()
    except Exception as e:
        logger.error("Voice plugin inject failed: %s", e)
        return {"error": str(e)}


@app.post("/voice/dispatch")
async def voice_dispatch(request: Request, req: VoiceDispatchRequest):
    """Inject voice text into Claude Code session via voice channel plugin."""
    if not _check_pin(request):
        return JSONResponse({"error": "Invalid PIN"}, status_code=401)

    target_channel, cleaned = _route_prefix(req.text)
    if req.channel_hint:
        target_channel = req.channel_hint

    # 冪等去重：iOS 喚醒後可能重放同一個 POST（見 dedup.py 模組說明）。
    # 命中時回傳首次結果並標 duplicate，絕不重複注入。
    cached = _dedup_cache.lookup(req.client_msg_id, cleaned)
    if cached is not None:
        logger.warning(
            "Voice inject duplicate suppressed: client_msg_id=%s text=%r",
            req.client_msg_id or "(text-hash)", cleaned[:60],
        )
        return {**cached, "duplicate": True}

    display_text = cleaned
    if target_channel != VOICE_CHANNEL_ID:
        display_text = f"[→ <#{target_channel}>] {cleaned}"

    logger.info("Voice inject: client_msg_id=%s text=%r", req.client_msg_id, cleaned[:60])
    t_start = time.time()

    result = await _dispatch_to_runtime(cleaned, req.runtime)
    elapsed = time.time() - t_start

    if "error" in result:
        logger.warning("Voice inject failed after %.1fs: %s", elapsed, result["error"])
        return JSONResponse({"injected": False, "error": result["error"], "elapsed": elapsed})

    if DISCORD_WEBHOOK:
        asyncio.create_task(_discord_post_webhook(display_text))

    message_id = result.get("message_id", "")
    response = {"injected": True, "message_id": message_id, "elapsed": elapsed}
    _dedup_cache.store(req.client_msg_id, cleaned, response)
    logger.info("Voice injected: message_id=%s elapsed=%.1fs", message_id, elapsed)
    return response


class RuntimeDispatchRequest(BaseModel):
    """Runtime adapter 協定的入站格式（與 gateway 對 mcp-bridge 的 POST 同形）。"""
    text: str
    callback_url: str
    user: str = ""


# create_task 的背景任務只留弱引用會被 GC 吃掉，這裡持有到完成為止
_fast_voice_tasks: set[asyncio.Task] = set()


@app.post("/runtime/fast-voice")
async def fast_voice_dispatch(request: Request, req: RuntimeDispatchRequest):
    """fast-voice runtime adapter：先回 message_id，回覆走 callback（協定同 mcp-bridge）。"""
    if not _check_pin(request):
        return JSONResponse({"error": "Invalid PIN"}, status_code=401)
    message_id = _fast_voice.next_message_id()
    task = asyncio.create_task(_fast_voice_run(req.text, req.callback_url, message_id))
    _fast_voice_tasks.add(task)
    task.add_done_callback(_fast_voice_tasks.discard)
    return {"message_id": message_id}


async def _fast_voice_run(text: str, callback_url: str, message_id: str) -> None:
    """背景判斷＋回投。reply 直接 callback；escalate（含灰喵故障 fallback）
    轉送 claude-code runtime，回覆靠 EscalationAliases 換回原 message_id。"""
    try:
        action, reply = await _fast_voice.decide(_http_client, text)
    except Exception as e:
        # 灰喵逾時或掛掉不能讓語音斷線——一律升級主 session（規劃 §4 fallback）
        logger.error("fast-voice decide failed, falling back to escalate: %s", e)
        action, reply = "escalate", None

    if action == "escalate":
        if await _escalate_to_claude_code(text, message_id):
            return
        reply = "語音快線和主系統都聯絡不上，這句我沒有處理到，麻煩稍後再試。"

    try:
        resp = await _http_client.post(
            callback_url,
            json={"text": reply, "message_id": message_id, "runtime_id": "fast-voice"},
            headers={"X-Voice-Pin": _BRIDGE_PIN},
        )
        if not resp.is_success:
            logger.error("fast-voice callback rejected: %s %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("fast-voice callback failed: %s", e)


async def _escalate_to_claude_code(text: str, original_id: str) -> bool:
    """把語音原文轉送 claude-code runtime，並把 bridge 自鑄的 message_id
    別名到原始 id 上（PWA 輪詢不換號）。成功回 True。"""
    runtime = _gateway_config.get("runtimes", {}).get("claude-code", {})
    url = runtime.get("url", f"{VOICE_PLUGIN_URL}/inject")
    if not runtime.get("enabled", True):
        logger.error("fast-voice escalate impossible: claude-code runtime disabled")
        return False
    try:
        resp = await _http_client.post(
            url,
            json={"text": text, "user": "Kevin"},
            headers={"X-Voice-Pin": _BRIDGE_PIN},
        )
        bridge_id = resp.json().get("message_id", "")
        if not resp.is_success or not bridge_id:
            logger.error("fast-voice escalate rejected: %s %s", resp.status_code, resp.text[:200])
            return False
    except Exception as e:
        logger.error("fast-voice escalate failed: %s", e)
        return False
    _escalation_aliases.register(bridge_id, original_id)
    logger.info("fast-voice escalated: %s -> %s text=%r", bridge_id, original_id, text[:40])
    return True


class VoiceReplyCallback(BaseModel):
    text: str
    message_id: str = ""
    runtime_id: str = ""


# 持久化回覆匣（票 6-2）：取代舊 _pending_replies 記憶體暫存。SQLite 落地、
# 24h/最近 20 筆滾動窗、ACK 三態（delivered→read→played）；server 重啟以此檔為
# 真相復原。
#
# lazy 單例（F5）：import 階段不建 DB，避免測試在 monkeypatch 前先碰真實家目錄。
# 首次使用（或啟動清理）才依 MEOWVOICE_REPLY_BOX_DB（預設 ~/.meowvoice/reply_box.db）
# 建立；測試可先設 env 或直接注入 server._reply_box。
_reply_box: ReplyBox | None = None
# 週期主動清理間隔（秒）：服務無流量時仍定時汰除逾期敏感內容（F3）。
REPLY_BOX_SWEEP_INTERVAL = float(os.environ.get("MEOWVOICE_REPLY_BOX_SWEEP_INTERVAL", "3600"))
_reply_box_sweeper_task: asyncio.Task | None = None


# 預合成音訊快取（票 6-3）：lazy 單例，同 F5 避免 import 階段碰真實家目錄。
_reply_cache: ReplyCache | None = None
# 點播端點的總等待預算（秒，H2）：await 合成 Future 逾時＝回 503＋Retry-After，
# 絕不開第二次合成（背景 job 續跑，快取完成後下次點播命中）。
REPLY_AUDIO_BUDGET = float(os.environ.get("MEOWVOICE_REPLY_AUDIO_BUDGET", "3.5"))


def _get_reply_cache() -> ReplyCache:
    """回 lazy 單例預合成快取（首次呼叫才建，讀 MEOWVOICE_REPLY_CACHE_DIR）。"""
    global _reply_cache
    if _reply_cache is None:
        cache_dir = Path(os.environ.get(
            "MEOWVOICE_REPLY_CACHE_DIR", str(_MEOWVOICE_DIR / "reply_cache")))
        _reply_cache = ReplyCache(cache_dir)
    return _reply_cache


def _get_reply_box() -> ReplyBox:
    """回 lazy 單例回覆匣（首次呼叫才建，讀 MEOWVOICE_REPLY_BOX_DB）。

    on_delete 掛到預合成快取的 delete：回覆被清出匣（到期／超窗）時，對應音訊
    快取檔連動刪除，快取生命週期與回覆匣一致（票 6-3）。"""
    global _reply_box
    if _reply_box is None:
        db_path = Path(os.environ.get(
            "MEOWVOICE_REPLY_BOX_DB", str(_MEOWVOICE_DIR / "reply_box.db")))
        _reply_box = ReplyBox(db_path, on_delete=_get_reply_cache().delete)
    return _reply_box


def _mlx_generate_wav(
    text: str,
    *,
    voice: str,
    temperature: float,
    speed: float,
    seed: int,
    lang: str = "chinese",
    stream: bool = False,
) -> bytes:
    """MLX in-process 生成完整 WAV bytes——/tts 與 reply worker 的共用核心（K3）。

    參數完整傳遞（voice／temperature／speed／seed／lang／stream）以與 /tts 對外
    行為等價；seed 固定聲線。回完整 WAV bytes，空音訊拋 RuntimeError 供呼叫端映射。
    為同步 CPU 工作——worker 路徑以 asyncio.to_thread 呼叫、不阻塞事件迴圈（K1）。"""
    model = load_tts()
    mx.random.seed(seed)
    audio_chunks: list[np.ndarray] = []
    for result in model.generate(
        text=text,
        voice=voice,
        lang_code=lang,
        verbose=False,
        stream=stream,
        temperature=temperature,
        speed=speed,
        **({"streaming_interval": 1.0} if stream else {}),
    ):
        if hasattr(result, "audio"):
            audio_chunks.append(np.array(result.audio))
    if not audio_chunks:
        raise RuntimeError("MLX 生成無音訊")
    audio_int16 = (np.concatenate(audio_chunks) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(model.sample_rate)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()


async def _synthesize_wav_bytes(text: str) -> bytes:
    """合成回覆全文為完整 WAV bytes——worker 的引擎無關合成 callable（J1）。

    依當前 TTS 引擎分派：CrispASR 走常駐服務、MLX 走 in-process 生成。序列化於
    _tts_lock 之下：兩引擎皆為單一資源，預合成／點播不與即時 /tts 併發搶佔。"""
    async with _tts_lock:
        if TTS_ENGINE != "crispasr":
            # K1：MLX 為同步 CPU 生成，移出事件迴圈（to_thread），否則整個迴圈——
            # health／其他端點／REPLY_AUDIO_BUDGET 計時器——全部停擺。_tts_lock 為
            # asyncio.Lock，跨 await 持有正常。
            return await asyncio.to_thread(
                _mlx_generate_wav,
                text,
                voice=TTS_VOICE,
                temperature=TTS_TEMPERATURE,
                speed=TTS_SPEED,
                seed=TTS_SEED,
            )
        if _crispasr is None:
            raise CrispasrSynthError("crispasr engine not active")
        # 進鎖後複查斷路器：排隊期間可能已跳閘，不讓 queued 請求再打死引擎。
        if _crispasr.is_unhealthy():
            raise CrispasrSynthError("circuit breaker open")
        result = await _crispasr.synth(_http_client, text)
    return _crispasr.pcm_to_wav(result.pcm)


async def _reply_box_sweeper() -> None:
    """週期喚醒回覆匣主動清理（見 ReplyBox.sweep）。由 lifespan 啟停，
    取消時吞 CancelledError 前先 re-raise 讓事件迴圈正常收束。"""
    while True:
        try:
            await asyncio.sleep(REPLY_BOX_SWEEP_INTERVAL)
            _get_reply_box().sweep()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("reply-box 週期清理失敗：%s", e)


@app.post("/voice/reply-callback")
async def voice_reply_callback(request: Request, req: VoiceReplyCallback):
    """Receive reply from any runtime adapter, persist to reply box for PWA pickup."""
    if not _check_pin(request):
        return JSONResponse({"error": "Invalid PIN"}, status_code=401)
    if not req.text.strip():
        return JSONResponse({"error": "Empty reply"}, status_code=400)

    source = req.runtime_id or "claude-code"
    logger.info("Voice reply [%s]: text=%r", source, req.text[:60])

    # 空 message_id 無法入匣取件，fail-loud 拒絕而非回 200 靜默吞（F4）。生產各
    # 回投路徑都帶 message_id（fast-voice next_message_id／bridge 自鑄 id）。
    if not req.message_id:
        logger.warning("Voice reply 缺 message_id，拒絕入匣：runtime=%s", source)
        return JSONResponse({"error": "message_id required"}, status_code=400)

    # 升級轉送的回覆帶 bridge 自鑄 id，換回 PWA 輪詢的原始 id 再入匣。
    store_id = _escalation_aliases.resolve(req.message_id)
    # Discord 與語音端共用同一 reply ID：撞 ID＝重複投遞，入匣擋下、也不重複發
    # Discord 保底（防雙投）。
    if not _get_reply_box().enqueue(store_id, req.text):
        return {"ok": False, "duplicate": True, "runtime": source}

    # 入匣成功即非同步觸發預合成（票 6-3）：快取盡力、不阻塞回覆——佇列滿載或
    # worker 未啟（MLX 模式）皆只跳過，回覆已落匣不受影響。await 僅為入列（notify
    # worker），不等合成。
    await _get_reply_cache().enqueue(store_id, req.text)

    if DISCORD_WEBHOOK:
        asyncio.create_task(_discord_post_webhook(f"🫧 {req.text}", f"青喵 (語音回覆/{source})"))

    return {"ok": True, "text_length": len(req.text), "runtime": source}


@app.get("/voice/reply/{message_id}")
async def get_reply(request: Request, message_id: str):
    """Poll for a voice reply by message_id.

    過渡相容：現行 iOS PWA 不發 ACK，故取件即自動 ACK 至 played（等效昔日 pop
    語意，防重播風暴）；已 played 者回 pending，不重播。顯式 ACK 端點與未讀列表
    留給票 6-4。"""
    if not _check_pin(request):
        return JSONResponse({"error": "Invalid PIN"}, status_code=401)
    text = _get_reply_box().claim_for_playback(message_id)
    if text is None:
        return {"status": "pending"}
    return {"status": "ready", "text": text}


@app.get("/voice/reply/{message_id}/audio")
async def get_reply_audio(request: Request, message_id: str):
    """點播回覆音訊（票 6-3）。

    命中預合成快取＝直接回檔（首音 <1s）；miss＝併入同一合成 job 等其結果
    （H1，端點不直接呼叫引擎）。不改 ACK 狀態——播放完成的 played ACK 屬票 6-4。

    狀態碼：200 音訊；404 reply 不存在／已到期；410 等待期間 reply 被刪；
    503＋Retry-After 逾時或引擎不可用（PWA 輪詢重試，背景 job 完成後命中）。"""
    if not _check_pin(request):
        return JSONResponse({"error": "Invalid PIN"}, status_code=401)
    # G1：先驗匣內有效再回音訊——已刪／到期一律 404，不回可能被背景 job 復活的快取。
    entry = _get_reply_box().get(message_id)
    if entry is None:
        return JSONResponse({"error": "reply not found"}, status_code=404)

    cache = _get_reply_cache()
    ready = cache.path_if_ready(message_id)
    if ready is not None:
        return FileResponse(ready, media_type="audio/wav")

    # H1/J1：合成一律經快取單一入口（點播高優先）、worker 依引擎分派（CrispASR／
    # MLX 皆然，端點不直呼引擎）；撞進行中的 job 回同一 Future 不重複做工。shield
    # 讓「等待逾時」只取消本次等待、不取消背景 job 的 Future。
    fut = await cache.get_or_join(message_id, entry[0], playback=True)
    waiter = asyncio.ensure_future(asyncio.shield(fut))
    try:
        done, _pending = await asyncio.wait({waiter}, timeout=REPLY_AUDIO_BUDGET)
    except asyncio.CancelledError:
        waiter.cancel()  # 本請求 task 被取消（client 斷線），清理後正常傳播（H3）
        raise
    if not done:
        # H2：逾時＝503＋Retry-After，絕不開第二次合成——背景 job 續跑，下次命中。
        waiter.cancel()
        return JSONResponse(
            {"error": "synthesis in progress", "detail": "retry shortly"},
            status_code=503, headers={"Retry-After": "1"})
    try:
        wav = waiter.result()
    except asyncio.CancelledError:
        # H3：Future 被取消——僅可能因 reply 於等待期間被刪（delete→fut.cancel）。
        # 回 410 與既有「已刪 ID」語意一致，不讓 CancelledError 洩漏成連線層錯誤。
        if fut.cancelled():
            return JSONResponse({"error": "reply deleted"}, status_code=410)
        raise
    except ReplyCacheFull:
        return JSONResponse(
            {"error": "synthesis queue full", "detail": "retry shortly"},
            status_code=503, headers={"Retry-After": "1"})
    except CrispasrSynthError as e:
        return JSONResponse({"error": "TTS engine unavailable", "detail": str(e)}, status_code=503)
    except Exception as e:
        logger.warning("點播合成失敗：id=%s err=%r", message_id, e)
        return JSONResponse({"error": "TTS failed"}, status_code=503)
    return StreamingResponse(io.BytesIO(wav), media_type="audio/wav")


class PinAuthRequest(BaseModel):
    pin: str


@app.post("/voice/auth")
async def pin_auth(request: Request, req: PinAuthRequest):
    """Authenticate with PIN, receive httpOnly session cookie."""
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        logger.warning("Rate limited: %s", client_ip)
        return JSONResponse({"error": "Too many attempts, try again later"}, status_code=429)
    if not _verify_pin(req.pin):
        _record_failed_attempt(client_ip)
        return JSONResponse({"error": "Invalid PIN"}, status_code=401)
    token = _create_session()
    response = JSONResponse({"ok": True, "expires_in": _SESSION_DURATION})
    response.set_cookie(
        "meowvoice_session", token,
        httponly=True, secure=True, samesite="strict",
        max_age=_SESSION_DURATION,
    )
    return response


class PinSetupRequest(BaseModel):
    pin: str


@app.post("/voice/pin/setup")
async def pin_setup(request: Request, req: PinSetupRequest):
    """First-time PIN setup. Restricted to localhost."""
    if _PIN_HASH_FILE.exists():
        return JSONResponse({"error": "PIN already configured"}, status_code=409)
    client_ip = request.client.host if request.client else ""
    if client_ip not in ("127.0.0.1", "::1"):
        return JSONResponse({"error": "PIN setup restricted to localhost"}, status_code=403)
    if len(req.pin) != 6 or not req.pin.isdigit():
        return JSONResponse({"error": "PIN must be exactly 6 digits"}, status_code=400)
    salt = secrets.token_bytes(32)
    _PIN_SALT_FILE.write_bytes(salt)
    os.chmod(str(_PIN_SALT_FILE), 0o600)
    _PIN_HASH_FILE.write_text(_hash_pin(req.pin, salt))
    os.chmod(str(_PIN_HASH_FILE), 0o600)
    logger.info("PIN setup completed from %s", client_ip)
    return {"ok": True}


@app.get("/voice/runtimes")
async def list_runtimes(request: Request):
    """List available runtime adapters and their status."""
    if not _check_pin(request):
        return JSONResponse({"error": "Invalid PIN"}, status_code=401)
    runtimes = _gateway_config.get("runtimes", {})
    result = {}
    for rid, r in runtimes.items():
        result[rid] = {
            "type": r.get("type", "unknown"),
            "enabled": r.get("enabled", False),
            "default": r.get("default", False),
            "description": r.get("description", ""),
        }
    return {"runtimes": result}


ALLOWED_CACHE_KEYS = frozenset(CACHED_PHRASES.keys())


@app.get("/voice/cached/{key}")
async def cached_voice(key: str):
    """Serve pre-cached voice feedback (ack/heartbeat/timeout)."""
    if key not in ALLOWED_CACHE_KEYS:
        return JSONResponse({"error": "Invalid cache key"}, status_code=400)
    data = _voice_cache.get(key)
    if not data:
        return JSONResponse({"error": "Voice not yet cached"}, status_code=503)
    return StreamingResponse(io.BytesIO(data), media_type="audio/wav")


SEED_PAGE = Path(__file__).parent / "seed-compare.html"


@app.get("/seeds", response_class=HTMLResponse)
async def seed_compare_page():
    """Seed comparison page for voice tuning."""
    if SEED_PAGE.exists():
        return HTMLResponse(SEED_PAGE.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>seed-compare.html not found</h1>", status_code=404)


@app.get("/", response_class=HTMLResponse)
async def test_page():
    """Browser-based voice test/conversation page for mobile/desktop."""
    if TEST_PAGE.exists():
        return HTMLResponse(TEST_PAGE.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>test-page.html not found</h1>", status_code=404)


if __name__ == "__main__":
    ssl_kwargs: dict = {}
    if Path(SSL_CERT).exists() and Path(SSL_KEY).exists():
        ssl_kwargs = {"ssl_certfile": SSL_CERT, "ssl_keyfile": SSL_KEY}
        logger.info("HTTPS enabled (cert=%s)", SSL_CERT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", **ssl_kwargs)
