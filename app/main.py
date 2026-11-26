from __future__ import annotations

import io
import subprocess
import json
import os
import shutil
import sqlite3
import threading
import time
import wave
from contextlib import asynccontextmanager, closing
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config
from .epub_parser import parse_epub
from .pipeline import TONES, tone_filter, normalize, load_pronunciations, apply_rules, SUPPORTED_EXTS, AlreadyExists, Cancelled, convert, safe, synth

DB_PATH = config.DATA_DIR / "jobs.db"
UPLOADS = config.DATA_DIR / "uploads"
WORK = config.DATA_DIR / "work"
_lock = threading.Lock()
_cancel: set[int] = set()
FALLBACK_VOICES = ["af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_adam", "am_michael",
                   "bf_emma", "bf_isabella", "bm_george", "bm_lewis"]


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    os.umask(config.UMASK)
    for d in (config.DATA_DIR, UPLOADS, WORK, config.OUTPUT_DIR, config.CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    with closing(db()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, upload TEXT, title TEXT, author TEXT,
            voice TEXT, speed REAL, status TEXT, done INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
            message TEXT DEFAULT '', output TEXT DEFAULT '', created REAL, finished REAL)""")
        try:
            c.execute("ALTER TABLE jobs ADD COLUMN meta TEXT DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass  # column already exists
        for col in ("started REAL", "eta REAL"):
            try:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        c.execute("UPDATE jobs SET status='queued' WHERE status='running'")  # resume after restart
        c.commit()


def update(job_id: int, **kw) -> None:
    cols = ", ".join(f"{k}=?" for k in kw)
    with _lock, closing(db()) as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*kw.values(), job_id))
        c.commit()


def next_job() -> sqlite3.Row | None:
    with closing(db()) as c:
        return c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()


def notify_abs() -> None:
    """Ask Audiobookshelf to scan its library (optional; failures never fail the job)."""
    if not (config.ABS_URL and config.ABS_API_KEY and config.ABS_LIBRARY_ID):
        return
    try:
        httpx.post(f"{config.ABS_URL}/api/libraries/{config.ABS_LIBRARY_ID}/scan",
                   headers={"Authorization": f"Bearer {config.ABS_API_KEY}"}, timeout=15)
    except Exception:  # noqa: BLE001
        pass


def worker_loop() -> None:
    while True:
        job = next_job()
        if not job:
            time.sleep(1.5)
            continue
        jid = job["id"]
        t0 = time.time()
        update(jid, status="running", message="Starting", started=t0, eta=None)
        last_write = [0.0]
        base = [None]  # chunks already cached at the start, so resumed jobs don't skew the estimate

        def on_progress(done: int, total: int, msg: str) -> None:
            now = time.time()
            if base[0] is None:
                base[0] = done
            if now - last_write[0] > 0.5 or done == total:
                last_write[0] = now
                fresh = done - base[0]
                eta = (now - t0) / fresh * (total - done) if fresh >= 3 and total > done else None
                update(jid, done=done, total=total, message=msg, eta=eta)

        def on_meta(book) -> None:
            update(jid, title=book.title, author=book.author)

        try:
            out = convert(Path(job["upload"]), WORK / str(jid), job["voice"], job["speed"],
                          on_progress, lambda: jid in _cancel, on_meta, json.loads(job["meta"] or "{}"))
            update(jid, status="done", message="Finished", output=str(out), finished=time.time())
            shutil.rmtree(WORK / str(jid), ignore_errors=True)
            notify_abs()
        except AlreadyExists as e:
            update(jid, status="done", message="Skipped: already in library", output=str(e.path),
                   finished=time.time())
            shutil.rmtree(WORK / str(jid), ignore_errors=True)
        except Cancelled:
            update(jid, status="cancelled", message="Cancelled")
            shutil.rmtree(WORK / str(jid), ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            update(jid, status="failed", message=str(e)[:400], finished=time.time())
        finally:
            _cancel.discard(jid)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    threading.Thread(target=worker_loop, daemon=True).start()
    yield


app = FastAPI(title="ebook2audio", lifespan=lifespan)


@app.get("/api/health")
def health():
    try:
        r = httpx.get(f"{config.TTS_URL}/health", timeout=5)
        tts_ok = r.status_code < 500
    except Exception:  # noqa: BLE001
        tts_ok = False
    return {"ok": True, "tts": tts_ok, "tts_url": config.TTS_URL}


@app.get("/api/voices")
def voices():
    try:
        r = httpx.get(f"{config.TTS_URL}/v1/audio/voices", timeout=8)
        r.raise_for_status()
        data = r.json()
        raw = data.get("voices", data) if isinstance(data, dict) else data
        v = sorted({(x.get("id") or x.get("name")) if isinstance(x, dict) else str(x) for x in raw} - {None})
        grades = {(x.get("id") or x.get("name")): x["overall_grade"] for x in raw
                  if isinstance(x, dict) and x.get("overall_grade")}
        if v:
            return {"voices": v, "grades": grades,
                    "default": config.DEFAULT_VOICE if config.DEFAULT_VOICE in v else v[0]}
    except Exception:  # noqa: BLE001
        pass
    return {"voices": FALLBACK_VOICES, "default": config.DEFAULT_VOICE, "fallback": True}


@app.post("/api/preview")
def preview(voice: str = Form(...), speed: float = Form(1.0), text: str = Form(""), tone: str = Form("natural")):
    text = text.strip()[:400] or "Chapter one. It was a bright cold day in April, and the clocks were striking thirteen."
    text = apply_rules(normalize(text), load_pronunciations())  # same text processing as real conversions
    try:
        with httpx.Client(timeout=60) as c:
            pcm = synth(c, text, voice, speed)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"TTS unavailable: {e}") from e
    if tone_filter(tone):
        r = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(config.SAMPLE_RATE), "-ac", "1",
                            "-i", "pipe:0", "-af", tone_filter(tone), "-f", "wav", "pipe:1"],
                           input=pcm, capture_output=True)
        if r.returncode != 0:
            raise HTTPException(500, f"Tone filter failed: {r.stderr.decode(errors='replace')[-200:]}")
        return Response(r.stdout, media_type="audio/wav")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(config.SAMPLE_RATE)
        w.writeframes(pcm)
    return Response(buf.getvalue(), media_type="audio/wav")


@app.post("/api/jobs")
async def create_jobs(files: list[UploadFile] = File(...), voice: str = Form(...), speed: float = Form(1.0),
                      author: str = Form(""), series: str = Form(""), narrator: str = Form(""),
                      tone: str = Form("natural")):
    voice = voice.strip()
    if not voice:
        raise HTTPException(400, "Choose a voice")
    speed = min(max(speed, 0.5), 2.0)
    made, rejected = [], []
    for f in files:
        name = f.filename or "book.epub"
        if Path(name).suffix.lower() not in SUPPORTED_EXTS:
            rejected.append(f"{name}: unsupported type (try {', '.join(sorted(SUPPORTED_EXTS))})")
            continue
        with _lock, closing(db()) as c:
            cur = c.execute("INSERT INTO jobs(filename, upload, voice, speed, status, created, title, author)"
                            " VALUES(?,?,?,?,?,?,?,?)",
                            (name, "", voice, speed, "queued", time.time(), Path(name).stem, ""))
            jid = cur.lastrowid
            dest = UPLOADS / f"{jid}_{safe(name, 100)}"
            c.execute("UPDATE jobs SET upload=?, status='uploading' WHERE id=?", (str(dest), jid))
            c.commit()
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out, 1024 * 1024)
        meta: dict = {}
        if dest.suffix.lower() == ".epub":  # show what the file already says, so edits are informed
            try:
                b = parse_epub(dest, meta_only=True)
                meta = {"title": b.title, "author": b.author, "series": b.series, "series_index": b.series_index}
            except Exception:  # noqa: BLE001
                pass
        # upload-wide values (apply to every book in this upload) beat what the file says
        for k, v in (("author", author), ("series", series), ("narrator", narrator)):
            if v.strip():
                meta[k] = v.strip()
        meta["tone"] = tone if tone in TONES else "natural"
        update(jid, status="queued", meta=json.dumps(meta), **({"title": meta["title"], "author": meta["author"]}
                                                              if meta.get("title") else {}))
        made.append(jid)
    return {"created": made, "rejected": rejected}


_gpu_cache: tuple[float, dict | None] = (0.0, None)


@app.get("/api/gpu")
def gpu():
    """GPU load via nvidia-smi (needs the NVIDIA runtime enabled for this container; null if unavailable)."""
    global _gpu_cache
    if time.time() - _gpu_cache[0] < 1.0:
        return _gpu_cache[1]
    info = None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=4)
        if out.returncode == 0 and out.stdout.strip():
            name, util, used, total, temp = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
            info = {"name": name.replace("NVIDIA ", ""), "util": int(util), "mem_used": int(used),
                    "mem_total": int(total), "temp": int(temp)}
    except Exception:  # noqa: BLE001
        info = None
    _gpu_cache = (time.time(), info)
    return info


@app.get("/api/jobs")
def list_jobs():
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 200").fetchall()
    now = time.time()  # server clock, so elapsed time is right even if the browser's clock is off
    return [{**dict(r), "now": now} for r in rows]


@app.patch("/api/jobs/{jid}/meta")
def edit_meta(jid: int, body: dict = Body(...)):
    with closing(db()) as c:
        row = c.execute("SELECT status, meta FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row["status"] not in ("queued", "failed", "cancelled"):
        raise HTTPException(409, "Only waiting or failed jobs can be edited")
    meta = json.loads(row["meta"] or "{}")
    for k in ("title", "author", "series", "series_index", "narrator"):
        if k in body:
            meta[k] = str(body[k] or "").strip()
    fields = {"meta": json.dumps(meta)}
    if meta.get("title"):
        fields["title"] = meta["title"]
    if meta.get("author"):
        fields["author"] = meta["author"]
    update(jid, **fields)
    return meta


@app.post("/api/jobs/{jid}/cancel")
def cancel(jid: int):
    with closing(db()) as c:
        row = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row["status"] == "queued":
        update(jid, status="cancelled", message="Cancelled")
    elif row["status"] == "running":
        _cancel.add(jid)
    return {"ok": True}


@app.post("/api/jobs/{jid}/retry")
def retry(jid: int):
    with closing(db()) as c:
        row = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row["status"] in ("failed", "cancelled"):
        update(jid, status="queued", message="Queued", finished=None)
    return {"ok": True}


@app.delete("/api/jobs/{jid}")
def remove(jid: int):
    with closing(db()) as c:
        row = c.execute("SELECT status, upload FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row["status"] == "running":
        raise HTTPException(409, "Cancel it first")
    with _lock, closing(db()) as c:
        c.execute("DELETE FROM jobs WHERE id=?", (jid,))
        c.commit()
    Path(row["upload"]).unlink(missing_ok=True)
    shutil.rmtree(WORK / str(jid), ignore_errors=True)
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
