"""MeowVoice 本地音訊服務 — TTS + STT + Voice Bridge (E-lite V2)

TTS: Qwen3-TTS 1.7B MLX, STT: Breeze-ASR-25 (MediaTek 台灣華語微調)
Voice Bridge: 語音文字 → TriClaw runtime dispatch → TTS
"""

import io
import os
import re
import json
import time
import wave
import asyncio
import tempfile
import logging
import urllib.request
import urllib.error
from contextlib import asynccontextmanager

from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse

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
DISPATCH_TIMEOUT = int(os.environ.get("MEOWVOICE_DISPATCH_TIMEOUT", "120"))

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
STT_GLOBAL_TERMS = "青喵、黑喵、貓爪、灰喵、小野、TriClaw、MeowVoice、Kevin"

CHANNEL_TERMS: dict[str, str] = {
    "1486183810143097093": "TriClaw、EventStore、correlation ID、SSE、Kernel、Runtime、Skill",
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


tts_model = None
stt_model_id = None
_mlx_lock = asyncio.Lock()


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
    load_tts()
    load_stt()
    _generate_cached_voices()
    voice_ok = bool(DISCORD_WEBHOOK and DISCORD_BOT_TOKEN)
    logger.info("Audio server ready on %s:%d (voice_bridge=%s, cached=%d)", HOST, PORT, voice_ok, len(_voice_cache))
    yield
    logger.info("Audio server shutting down")


app = FastAPI(title="MeowVoice Audio Server", lifespan=lifespan)
ALLOWED_ORIGINS = os.environ.get(
    "MEOWVOICE_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,https://127.0.0.1:8400",
).split(",")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"])

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
    async with _mlx_lock:
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
    file: UploadFile = File(..., description="WAV audio file"),
    lang: str = Query(default="zh", description="Language hint"),
    channel_id: str = Query(default="", description="Channel ID for terminology prompt"),
):
    """Transcribe audio to text using mlx-whisper with dynamic terminology."""
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
        async with _mlx_lock:
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
    file: UploadFile = File(...),
    model: str = Query(default="breeze-asr-25"),
    language: str = Query(default="zh"),
    channel_id: str = Query(default="", alias="channel_id"),
):
    """OpenAI-compatible STT endpoint with dynamic terminology."""
    result = await stt_transcribe(file=file, lang=language, channel_id=channel_id)
    return {"text": result["text"]}


class ChatCompletionRequest(BaseModel):
    model: str = "triclaw-dispatch"
    messages: list[dict]
    temperature: float = 0.7
    max_tokens: int | None = None
    stream: bool = False


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    """OpenAI-compatible chat endpoint backed by TriClaw dispatch."""
    user_messages = [m["content"] for m in req.messages if m.get("role") == "user"]
    if not user_messages:
        return JSONResponse({"error": "No user message"}, status_code=400)

    prompt_text = user_messages[-1]
    reply = await _triclaw_dispatch(prompt_text)

    if not reply:
        return JSONResponse(
            {"error": {"message": "TriClaw dispatch failed", "type": "server_error"}},
            status_code=502,
        )

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


_DISCORD_HEADERS = {"Content-Type": "application/json", "User-Agent": "MeowVoice/1.0"}


def _discord_post_webhook(text: str, username: str = "Kevin (語音)") -> dict | None:
    """Post a message via Discord webhook. Returns the created message."""
    if not DISCORD_WEBHOOK:
        return None
    url = DISCORD_WEBHOOK + "?wait=true"
    data = json.dumps({"content": text, "username": username}).encode()
    req = urllib.request.Request(url, data=data, headers=_DISCORD_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.error("Webhook post failed: %s", e)
        return None


def _discord_fetch_replies(channel_id: str, after_id: str) -> list[dict]:
    """Fetch messages in a channel after a given message ID."""
    if not DISCORD_BOT_TOKEN:
        return []
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?after={after_id}&limit=20"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "User-Agent": "MeowVoice/1.0"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.error("Discord fetch failed: %s", e)
        return []


class VoiceDispatchRequest(BaseModel):
    text: str
    channel_hint: str = ""


_dispatch_lock = asyncio.Lock()

VOICE_SYSTEM_PROMPT = """你是青喵（CyanMeow），Kevin 的 AI 主控核心。現在是語音對話模式。
回覆規則：繁體中文、口語化、2-4 句話以內、先結論再補充、不用 markdown。"""

TRICLAW_BINARY = os.environ.get("MEOWVOICE_TRICLAW_BINARY", "triclaw")
TRICLAW_RUNTIME = os.environ.get("MEOWVOICE_TRICLAW_RUNTIME", "claude")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TRACING_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+.*\s(?:INFO|WARN|ERROR|DEBUG|TRACE)\s")
_ROUTED_RE = re.compile(r"^routed to \w+.*confidence \d", re.IGNORECASE)


def _strip_ansi_and_log_lines(text: str) -> str:
    """Remove ANSI escape codes and tracing log lines from triclaw output."""
    cleaned = _ANSI_RE.sub("", text)
    lines = [
        line for line in cleaned.splitlines()
        if not _TRACING_LINE_RE.match(line.strip())
        and not _ROUTED_RE.match(line.strip())
    ]
    return "\n".join(lines).strip()


async def _triclaw_dispatch(text: str) -> str | None:
    """Dispatch voice text via TriClaw runtime (model routing + failover + EventStore)."""
    prompt = f"{VOICE_SYSTEM_PROMPT}\n\n使用者說：{text}"
    _DISPATCH_ENV_ALLOW = [
        "PATH", "HOME", "USER", "SHELL", "LANG", "TERM",
        "TRICLAW_HOME", "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_EXECPATH", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    ]
    env = {"RUST_LOG": "error"}
    for k in _DISPATCH_ENV_ALLOW:
        v = os.environ.get(k, "")
        if v:
            env[k] = v
    proc = await asyncio.create_subprocess_exec(
        TRICLAW_BINARY, "runtime", "dispatch", prompt,
        "--runtime", TRICLAW_RUNTIME,
        "--timeout", str(DISPATCH_TIMEOUT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=DISPATCH_TIMEOUT + 10,
        )
        if proc.returncode == 0 and stdout:
            return _strip_ansi_and_log_lines(stdout.decode())
        logger.error(
            "triclaw dispatch failed: rc=%s stdout=%s stderr=%s",
            proc.returncode,
            stdout.decode()[:200] if stdout else "(empty)",
            stderr.decode()[:200] if stderr else "(empty)",
        )
        return None
    except asyncio.TimeoutError:
        proc.kill()
        logger.error("triclaw dispatch timeout after %ds", DISPATCH_TIMEOUT + 10)
        return None


@app.post("/voice/dispatch")
async def voice_dispatch(req: VoiceDispatchRequest):
    """Process voice text via TriClaw runtime dispatch."""
    async with _dispatch_lock:
        return await _do_dispatch(req)


async def _do_dispatch(req: VoiceDispatchRequest) -> dict:
    target_channel, cleaned = _route_prefix(req.text)
    if req.channel_hint:
        target_channel = req.channel_hint

    display_text = cleaned
    if target_channel != VOICE_CHANNEL_ID:
        display_text = f"[→ <#{target_channel}>] {cleaned}"

    # Audit trail: post to Discord (fire-and-forget)
    if DISCORD_WEBHOOK:
        asyncio.get_running_loop().run_in_executor(None, _discord_post_webhook, display_text)

    logger.info("Voice dispatch: text=%r context=%s", cleaned[:40], target_channel)
    t_start = time.time()

    reply_text = await _triclaw_dispatch(cleaned)

    elapsed = time.time() - t_start
    if not reply_text:
        logger.warning("Voice dispatch failed after %.1fs", elapsed)
        return JSONResponse({"text": "", "timeout": True, "elapsed": elapsed})

    # Audit trail: post response to Discord
    if DISCORD_WEBHOOK:
        asyncio.get_running_loop().run_in_executor(
            None, _discord_post_webhook, f"🫧 {reply_text}", "青喵 (語音回覆)"
        )

    logger.info("Voice reply: text=%r elapsed=%.1fs", reply_text[:50], elapsed)
    return {"text": reply_text, "timeout": False, "elapsed": elapsed}


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


@app.get("/test", response_class=HTMLResponse)
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
