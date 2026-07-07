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

import numpy as np
import uvicorn
from fastapi import FastAPI, Request, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
import httpx

logger = logging.getLogger("meowvoice-audio")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


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

DISCORD_WEBHOOK = os.environ.get("MEOWVOICE_DISCORD_WEBHOOK", "")
VOICE_CHANNEL_ID = os.environ.get("MEOWVOICE_VOICE_CHANNEL_ID", "1475645959542145166")
CYANMEOW_BOT_ID = os.environ.get("MEOWVOICE_CYANMEOW_BOT_ID", "1490193787463532724")
DISPATCH_TIMEOUT = int(os.environ.get("MEOWVOICE_DISPATCH_TIMEOUT", "60"))
VOICE_PIN = os.environ.get("MEOWVOICE_PIN", "")
VOICE_PLUGIN_URL = os.environ.get("MEOWVOICE_VOICE_PLUGIN", "http://127.0.0.1:8401")

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


def load_tts():
    global tts_model
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


def _generate_cached_voices() -> None:
    """Pre-generate standard voice feedback phrases at startup."""
    model = load_tts()
    for key, phrase in CACHED_PHRASES.items():
        t0 = time.time()
        audio_chunks = []
        for result in model.generate(
            text=phrase, voice=TTS_VOICE, lang_code="zh",
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
    global _http_client
    _http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    _init_pin_storage()
    load_tts()
    load_stt()
    _generate_cached_voices()
    voice_ok = bool(DISCORD_WEBHOOK and DISCORD_BOT_TOKEN)
    logger.info("Audio server ready on %s:%d (voice_bridge=%s, cached=%d)", HOST, PORT, voice_ok, len(_voice_cache))
    yield
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


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "tts_model": TTS_MODEL_ID,
        "tts_loaded": tts_model is not None,
        "stt_model": STT_MODEL_ID,
        "tts_sample_rate": tts_model.sample_rate if tts_model else None,
    }


from pydantic import BaseModel

class TtsRequest(BaseModel):
    text: str
    voice: str = TTS_VOICE
    lang: str = "zh"
    # stream 保留供 model.generate() 內部分段用，HTTP 回應一律為完整 WAV
    stream: bool = False

@app.post("/tts")
async def tts_generate(req: TtsRequest):
    """Generate speech from text. Returns WAV audio."""
    async with _tts_lock:
        return await _tts_generate_inner(req)


async def _tts_generate_inner(req: TtsRequest):
    model = load_tts()
    text, voice, lang, stream = req.text, req.voice, req.lang, req.stream
    t_start = time.time()

    # MLX Metal tensors are bound to the main-thread GPU stream (stream 0).
    # All generation must run on the event-loop thread — acceptable for single-user local server.
    audio_chunks: list[np.ndarray] = []
    chunk_count = 0
    for result in model.generate(
        text=text,
        voice=voice,
        lang_code=lang,
        verbose=False,
        stream=stream,
        **({"streaming_interval": 1.0} if stream else {}),
    ):
        if hasattr(result, "audio"):
            audio_chunks.append(np.array(result.audio))
            chunk_count += 1

    if not audio_chunks:
        return JSONResponse({"error": "No audio generated"}, status_code=500)

    logger.info("TTS done: text=%r chunks=%d time=%.2fs", text[:30], chunk_count, time.time() - t_start)

    full_audio = np.concatenate(audio_chunks)
    audio_int16 = (full_audio * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(model.sample_rate)
        wf.writeframes(audio_int16.tobytes())
    buf.seek(0)
    return StreamingResponse(buf, media_type="audio/wav")


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
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        prompt = build_stt_prompt(channel_id)
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
            headers={"X-Voice-Pin": VOICE_PIN},
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
            headers={"X-Voice-Pin": VOICE_PIN},
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

    display_text = cleaned
    if target_channel != VOICE_CHANNEL_ID:
        display_text = f"[→ <#{target_channel}>] {cleaned}"

    if DISCORD_WEBHOOK:
        asyncio.create_task(_discord_post_webhook(display_text))

    logger.info("Voice inject: text=%r", cleaned[:60])
    t_start = time.time()

    result = await _dispatch_to_runtime(cleaned, req.runtime)
    elapsed = time.time() - t_start

    if "error" in result:
        logger.warning("Voice inject failed after %.1fs: %s", elapsed, result["error"])
        return JSONResponse({"injected": False, "error": result["error"], "elapsed": elapsed})

    message_id = result.get("message_id", "")
    logger.info("Voice injected: message_id=%s elapsed=%.1fs", message_id, elapsed)
    return {"injected": True, "message_id": message_id, "elapsed": elapsed}


class VoiceReplyCallback(BaseModel):
    text: str
    message_id: str = ""
    runtime_id: str = ""


@app.post("/voice/reply-callback")
async def voice_reply_callback(request: Request, req: VoiceReplyCallback):
    """Receive reply from any runtime adapter, log and optionally trigger TTS."""
    if not _check_pin(request):
        return JSONResponse({"error": "Invalid PIN"}, status_code=401)
    if not req.text.strip():
        return JSONResponse({"error": "Empty reply"}, status_code=400)

    source = req.runtime_id or "claude-code"
    logger.info("Voice reply [%s]: text=%r", source, req.text[:60])

    if DISCORD_WEBHOOK:
        asyncio.create_task(_discord_post_webhook(f"🫧 {req.text}", f"青喵 (語音回覆/{source})"))

    return {"ok": True, "text_length": len(req.text), "runtime": source}


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
