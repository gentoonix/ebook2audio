"""Fake Kokoro-FastAPI: returns a tone of length proportional to the text (raw s16le 24 kHz)."""
import asyncio
import math
import os
import struct

from fastapi import FastAPI, Request
from fastapi.responses import Response

app = FastAPI()
calls = {"n": 0}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/audio/voices")
def voices():
    return {"voices": [{"id": "af_bella", "name": "af_bella", "overall_grade": "A-"}, {"id": "am_adam", "name": "am_adam"}, {"id": "zf_xiaoxiao", "name": "zf_xiaoxiao"}]}


@app.post("/v1/audio/speech")
async def speech(req: Request):
    body = await req.json()
    assert body["response_format"] == "pcm"
    calls["n"] += 1
    await asyncio.sleep(float(os.getenv("MOCK_DELAY", "0")))
    n = int(24000 * max(0.3, len(body["input"]) / 15.0))
    freq = 220 + (hash(body["voice"]) % 200)
    data = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / 24000))) for i in range(0, n))
    return Response(data, media_type="audio/pcm")


@app.get("/calls")
def get_calls():
    return calls
