"""MeowVoice 本地音訊服務 — TTS (Qwen3-TTS 1.7B MLX) + STT (mlx-whisper)

AIRI Electron 透過 localhost HTTP 呼叫此服務。
TTS 使用 SSE 串流回傳音訊 chunks，STT 接收 WAV 回傳文字。
"""

import io
import os
import time
import wave
import tempfile
import logging
from contextlib import asynccontextmanager

import ssl
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse

logger = logging.getLogger("meowvoice-audio")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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
    logger.info("Audio server ready on %s:%d", HOST, PORT)
    yield
    logger.info("Audio server shutting down")


app = FastAPI(title="MeowVoice Audio Server", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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


from pydantic import BaseModel, Field

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

    if stream:
        def generate_chunks():
            chunk_idx = 0
            for result in model.generate(
                text=text,
                voice=voice,
                lang_code=lang,
                verbose=False,
                stream=True,
                streaming_interval=1.0,
            ):
                if hasattr(result, "audio"):
                    audio_np = np.array(result.audio)
                    audio_int16 = (audio_np * 32767).astype(np.int16)

                    buf = io.BytesIO()
                    with wave.open(buf, "w") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(model.sample_rate)
                        wf.writeframes(audio_int16.tobytes())

                    chunk_data = buf.getvalue()
                    header = len(chunk_data).to_bytes(4, "big")
                    yield header + chunk_data
                    chunk_idx += 1

            logger.info("TTS done: text=%r chunks=%d time=%.2fs", text[:30], chunk_idx, time.time() - t_start)

        return StreamingResponse(
            generate_chunks(),
            media_type="application/octet-stream",
            headers={"X-Sample-Rate": str(model.sample_rate)},
        )
    else:
        chunks = []
        for result in model.generate(
            text=text,
            voice=voice,
            lang_code=lang,
            verbose=False,
            stream=False,
        ):
            if hasattr(result, "audio"):
                chunks.append(np.array(result.audio))

        if not chunks:
            return JSONResponse({"error": "No audio generated"}, status_code=500)

        full_audio = np.concatenate(chunks)
        audio_int16 = (full_audio * 32767).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(model.sample_rate)
            wf.writeframes(audio_int16.tobytes())
        buf.seek(0)

        logger.info("TTS done: text=%r time=%.2fs", text[:30], time.time() - t_start)
        return StreamingResponse(buf, media_type="audio/wav")


@app.post("/stt")
async def stt_transcribe(
    file: UploadFile = File(..., description="WAV audio file"),
    lang: str = Query(default="zh", description="Language hint"),
):
    """Transcribe audio to text using mlx-whisper."""
    import mlx_whisper

    t_start = time.time()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
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


@app.get("/test", response_class=HTMLResponse)
async def test_page():
    """Browser-based voice test page for mobile/desktop."""
    if TEST_PAGE.exists():
        return HTMLResponse(TEST_PAGE.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>test-page.html not found</h1>", status_code=404)


if __name__ == "__main__":
    ssl_kwargs: dict = {}
    if Path(SSL_CERT).exists() and Path(SSL_KEY).exists():
        ssl_kwargs = {"ssl_certfile": SSL_CERT, "ssl_keyfile": SSL_KEY}
        logger.info("HTTPS enabled (cert=%s)", SSL_CERT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", **ssl_kwargs)
