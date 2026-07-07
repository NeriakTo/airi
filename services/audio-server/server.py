"""MeowVoice 本地音訊服務 — TTS + STT + Voice Bridge

TTS: Qwen3-TTS 1.7B MLX, STT: mlx-whisper large-v3-turbo
Voice Bridge: 語音文字 → Discord webhook → 輪詢 CyanMeow 回覆 → TTS
"""

import io
import os
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
    "mlx-community/whisper-large-v3-turbo",
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

tts_model = None
stt_model_id = None


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_tts()
    load_stt()
    voice_ok = bool(DISCORD_WEBHOOK and DISCORD_BOT_TOKEN)
    logger.info("Audio server ready on %s:%d (voice_bridge=%s)", HOST, PORT, voice_ok)
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
    stream: bool = True

@app.post("/tts")
async def tts_generate(req: TtsRequest):
    """Generate speech from text. Returns WAV audio (streaming via chunked transfer)."""
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
):
    """Transcribe audio to text using mlx-whisper."""
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
        result = mlx_whisper.transcribe(
            tmp_path,
            path_or_hf_repo=stt_model_id,
            language=lang,
            verbose=False,
            initial_prompt="以下是繁體中文語音內容的轉錄。青喵、灰喵、黑喵、貓爪、小野是 AI 助手的名字。",
            condition_on_previous_text=False,
        )
        text = result.get("text", "").strip()
        duration = time.time() - t_start
        logger.info("STT done: text=%r time=%.2fs", text[:50], duration)
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
            {"id": "whisper-large-v3-turbo", "object": "model", "owned_by": "meowvoice"},
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
    model: str = Query(default="whisper-large-v3-turbo"),
    language: str = Query(default="zh"),
):
    """OpenAI-compatible STT endpoint for AIRI xsai integration."""
    result = await stt_transcribe(file=file, lang=language)
    return {"text": result["text"]}


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


async def _claude_cli_process(text: str) -> str | None:
    """Call claude CLI in one-shot mode to process voice text."""
    prompt = f"{VOICE_SYSTEM_PROMPT}\n\n使用者說：{text}"
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", "--max-turns", "3", "--output-format", "text",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode()),
            timeout=DISPATCH_TIMEOUT,
        )
        if proc.returncode == 0 and stdout:
            return stdout.decode().strip()
        logger.error(
            "claude CLI failed: rc=%s stdout=%s stderr=%s",
            proc.returncode,
            stdout.decode()[:200] if stdout else "(empty)",
            stderr.decode()[:200] if stderr else "(empty)",
        )
        return None
    except asyncio.TimeoutError:
        proc.kill()
        logger.error("claude CLI timeout after %ds", DISPATCH_TIMEOUT)
        return None


@app.post("/voice/dispatch")
async def voice_dispatch(req: VoiceDispatchRequest):
    """Process voice text via claude CLI and return response."""
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
        asyncio.get_event_loop().run_in_executor(None, _discord_post_webhook, display_text)

    logger.info("Voice dispatch: text=%r context=%s", cleaned[:40], target_channel)
    t_start = time.time()

    reply_text = await _claude_cli_process(cleaned)

    elapsed = time.time() - t_start
    if not reply_text:
        logger.warning("Voice dispatch failed after %.1fs", elapsed)
        return JSONResponse({"text": "", "timeout": True, "elapsed": elapsed})

    # Audit trail: post response to Discord
    if DISCORD_WEBHOOK:
        asyncio.get_event_loop().run_in_executor(
            None, _discord_post_webhook, f"🫧 {reply_text}", "青喵 (語音回覆)"
        )

    logger.info("Voice reply: text=%r elapsed=%.1fs", reply_text[:50], elapsed)
    return {"text": reply_text, "timeout": False, "elapsed": elapsed}


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
