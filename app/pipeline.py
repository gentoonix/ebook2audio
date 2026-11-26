"""Chunking, TTS calls, and M4B assembly. Resumable: finished chunks/chapters are cached on disk."""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import httpx

from . import config
from .epub_parser import Book, parse_epub

ABBREV = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "prof", "vs", "etc", "no", "mt", "col", "gen", "capt", "lt", "sgt"}
SENT_SPLIT = re.compile(r'(?<=[.!?…]["”’)\]])\s+|(?<=[.!?…])\s+')


class Cancelled(Exception):
    pass


class AlreadyExists(Exception):
    def __init__(self, path: Path):
        super().__init__(str(path))
        self.path = path


# ---------- text prep ----------

def load_pronunciations() -> list[tuple[re.Pattern, str]]:
    rules = []
    f = config.CONFIG_DIR / "pronunciation.txt"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            src, dst = line.split("=", 1)
            src, dst = src.strip(), dst.strip()
            if src:
                rules.append((re.compile(r"(?<!\w)" + re.escape(src) + r"(?!\w)"), dst))
    return rules


def apply_rules(text: str, rules) -> str:
    for pat, dst in rules:
        text = pat.sub(dst, text)
    return text


def split_sentences(par: str) -> list[str]:
    parts = SENT_SPLIT.split(par)
    merged: list[str] = []
    for p in parts:
        if merged:
            last_word = merged[-1].rstrip().split(" ")[-1].rstrip(".").lower()
            if last_word in ABBREV and merged[-1].rstrip().endswith("."):
                merged[-1] += " " + p
                continue
        merged.append(p)
    return merged


def _split_long(s: str, limit: int) -> list[str]:
    if len(s) <= limit:
        return [s]
    out, cur = [], ""
    for piece in re.split(r"(?<=[,;:—–-])\s+", s):
        if len(piece) > limit:  # no punctuation: hard split on words
            for w in piece.split(" "):
                if len(cur) + len(w) + 1 > limit and cur:
                    out.append(cur)
                    cur = w
                else:
                    cur = f"{cur} {w}".strip()
            continue
        if len(cur) + len(piece) + 1 > limit and cur:
            out.append(cur)
            cur = piece
        else:
            cur = f"{cur} {piece}".strip()
    if cur:
        out.append(cur)
    return out


def chunk_paragraphs(paragraphs: list[str], max_chars: int) -> list[tuple[str, int]]:
    """-> [(text, pause_after_ms)]"""
    chunks: list[tuple[str, int]] = []
    for par in paragraphs:
        cur = ""
        local: list[str] = []
        for s in split_sentences(par):
            for piece in _split_long(s, max_chars):
                if cur and len(cur) + len(piece) + 1 > max_chars:
                    local.append(cur)
                    cur = piece
                else:
                    cur = f"{cur} {piece}".strip()
        if cur:
            local.append(cur)
        for i, t in enumerate(local):
            last = i == len(local) - 1
            chunks.append((t, config.PARAGRAPH_PAUSE_MS if last else config.SENTENCE_PAUSE_MS))
    return chunks


# ---------- TTS ----------

def synth(client: httpx.Client, text: str, voice: str, speed: float) -> bytes:
    payload = {
        "model": config.TTS_MODEL,
        "input": text,
        "voice": voice,
        "speed": speed,
        "response_format": "pcm",  # raw s16le mono, no header parsing needed
    }
    last: Exception | None = None
    for attempt in range(4):
        try:
            r = client.post(f"{config.TTS_URL}/v1/audio/speech", json=payload)
            r.raise_for_status()
            if len(r.content) < 200:
                raise RuntimeError("TTS returned (almost) no audio")
            return r.content
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"TTS failed after retries: {last}")


def silence(ms: int) -> bytes:
    return b"\x00\x00" * int(config.SAMPLE_RATE * ms / 1000)


# ---------- output paths ----------

def safe(s: str, limit: int = 150) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit].strip(" .") or "Unknown"


def output_path(book: Book) -> Path:
    fields = {
        "author": safe(book.author) if book.author else "",
        "title": safe(book.title),
        "series": safe(book.series) if book.series else "",
        "seq": f"Vol {book.series_index}" if book.series and book.series_index else "",
        "year": book.year,
    }
    segs = []
    for seg in config.OUTPUT_PATTERN.split("/"):
        s = seg.format(**fields)
        s = re.sub(r"\(\s*\)|\[\s*\]", "", s)
        s = re.sub(r"(?:\s*-\s*){2,}", " - ", s)
        s = re.sub(r"\s+", " ", s).strip(" -")
        if s:
            segs.append(safe(s))
    folder = config.OUTPUT_DIR.joinpath(*segs)
    return folder / f"{safe(book.title)}.m4b"


def _esc(s: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", s)


# ---------- format conversion (Calibre) ----------

SUPPORTED_EXTS = {".epub", ".mobi", ".azw", ".azw3", ".fb2", ".pdf", ".txt", ".docx", ".rtf",
                  ".html", ".htm", ".odt"}
HEURISTIC_EXTS = {".pdf", ".txt", ".rtf", ".html", ".htm"}  # no real chapter structure to start from


def to_epub(src: Path, work: Path) -> Path:
    """Convert any supported ebook to EPUB with Calibre's ebook-convert. Cached for resumes."""
    out = work / "converted.epub"
    if out.exists() and out.stat().st_size > 0:
        return out
    if shutil.which("ebook-convert") is None:
        raise RuntimeError("Calibre (ebook-convert) is not installed in this container")
    cmd = ["ebook-convert", str(src), str(out.with_suffix(".tmp.epub"))]
    if src.suffix.lower() in HEURISTIC_EXTS:
        cmd += ["--enable-heuristics"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=config.CONVERT_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Conversion timed out after {config.CONVERT_TIMEOUT}s") from e
    tmp = out.with_suffix(".tmp.epub")
    if r.returncode != 0 or not tmp.exists():
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        hint = " (DRM-protected files can't be converted)" if any("DRM" in m for m in msg) else ""
        raise RuntimeError(f"Conversion failed{hint}: {(msg[-1] if msg else 'unknown error')[:200]}")
    tmp.rename(out)
    return out


# ---------- main entry ----------

def convert(
    epub_path: Path,
    work: Path,
    voice: str,
    speed: float,
    on_progress: Callable[[int, int, str], None],
    cancelled: Callable[[], bool],
    on_meta: Callable[[Book], None] | None = None,
    overrides: dict | None = None,
) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    if epub_path.suffix.lower() != ".epub":
        on_progress(0, 0, f"Converting {epub_path.suffix.lower()} to EPUB")
        epub_path = to_epub(epub_path, work)
    book = parse_epub(epub_path)
    for k in ("title", "author", "series", "series_index", "narrator"):
        if overrides and k in overrides:
            setattr(book, k, str(overrides[k] or "").strip())
    book.title = book.title or epub_path.stem
    book.author = book.author or "Unknown Author"
    book.narrator = book.narrator or config.NARRATOR.format(voice=voice)
    if on_meta:
        on_meta(book)
    target = output_path(book)
    if target.exists() and not config.OVERWRITE:
        raise AlreadyExists(target)
    rules = load_pronunciations()

    plans: list[list[tuple[str, int]]] = []
    for ch in book.chapters:
        paras = list(ch.paragraphs)
        if config.ANNOUNCE_CHAPTERS:
            paras.insert(0, ch.title if re.search(r"[.!?]$", ch.title) else ch.title + ".")
        paras = [apply_rules(p, rules) for p in paras]
        plans.append(chunk_paragraphs(paras, config.MAX_CHARS))

    total = sum(len(p) for p in plans)
    done = 0
    on_progress(done, total, "Starting")

    timeout = httpx.Timeout(300.0, connect=15.0)
    with httpx.Client(timeout=timeout) as client:
        for ci, (ch, plan) in enumerate(zip(book.chapters, plans)):
            cdir = work / f"c{ci:03d}"
            cdir.mkdir(exist_ok=True)
            chapter_pcm = cdir / "chapter.pcm"
            if chapter_pcm.exists():
                done += len(plan)
                on_progress(done, total, f"Chapter {ci + 1}/{len(book.chapters)} (cached)")
                continue

            def work_one(idx: int, text: str, _cdir=cdir) -> int:
                if cancelled():
                    raise Cancelled()
                f = _cdir / f"{idx:05d}.pcm"
                if not (f.exists() and f.stat().st_size > 0):
                    data = synth(client, text, voice, speed)
                    tmp = f.with_suffix(".tmp")
                    tmp.write_bytes(data)
                    tmp.rename(f)
                return idx

            with ThreadPoolExecutor(max_workers=config.TTS_CONCURRENCY) as ex:
                futs = [ex.submit(work_one, i, t) for i, (t, _p) in enumerate(plan)]
                try:
                    for fu in futs:
                        fu.result()
                        done += 1
                        on_progress(done, total, f"Chapter {ci + 1}/{len(book.chapters)}: {ch.title[:40]}")
                except BaseException:
                    for fu in futs:
                        fu.cancel()
                    raise
            if cancelled():
                raise Cancelled()

            tmp = cdir / "chapter.tmp"
            with open(tmp, "wb") as out:
                for i, (_t, pause) in enumerate(plan):
                    out.write((cdir / f"{i:05d}.pcm").read_bytes())
                    out.write(silence(pause))
            tmp.rename(chapter_pcm)
            for f in cdir.glob("0*.pcm"):
                f.unlink()

    return assemble(book, work)


def assemble(book: Book, work: Path) -> Path:
    bytes_per_ms = config.SAMPLE_RATE * 2 / 1000
    pcms = [work / f"c{i:03d}" / "chapter.pcm" for i in range(len(book.chapters))]

    meta = [";FFMETADATA1", f"title={_esc(book.title)}", f"artist={_esc(book.author)}",
            f"album_artist={_esc(book.author)}", f"album={_esc(book.title)}",
            f"composer={_esc(book.narrator)}", "genre=Audiobook"]
    if book.description:
        meta.append(f"description={_esc(book.description[:1000])}")
    if book.year:
        meta.append(f"date={book.year}")
    t = 0
    for ch, p in zip(book.chapters, pcms):
        dur = int(p.stat().st_size / bytes_per_ms)
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={t}", f"END={t + dur}", f"title={_esc(ch.title)}"]
        t += dur
    meta_file = work / "ffmeta.txt"
    meta_file.write_text("\n".join(meta) + "\n", encoding="utf-8")

    final = output_path(book)
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_suffix(".part.m4b")

    cover_file = None
    if book.cover:
        cover_file = work / f"cover.{book.cover_ext}"
        cover_file.write_bytes(book.cover)

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "s16le", "-ar", str(config.SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
           "-i", str(meta_file)]
    if cover_file:
        cmd += ["-i", str(cover_file)]
    cmd += ["-map", "0:a"]
    if cover_file:
        cmd += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    cmd += ["-map_metadata", "1", "-map_chapters", "1",
            "-c:a", "aac", "-b:a", config.BITRATE, "-movflags", "+faststart", "-f", "mp4", str(part)]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert proc.stdin
        for p in pcms:
            with open(p, "rb") as fh:
                shutil.copyfileobj(fh, proc.stdin, 1024 * 1024)
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg failed: {err[-500:]}")
    except BaseException:
        proc.kill()
        part.unlink(missing_ok=True)
        raise
    part.replace(final)
    write_sidecars(book, final.parent)
    return final


def write_sidecars(book: Book, folder: Path) -> None:
    """Files Audiobookshelf reads from the book folder: cover, reader.txt, desc.txt, metadata.opf."""
    if book.cover:
        (folder / f"cover.{book.cover_ext}").write_bytes(book.cover)
    (folder / "reader.txt").write_text(book.narrator + "\n", encoding="utf-8")
    if book.description:
        (folder / "desc.txt").write_text(book.description + "\n", encoding="utf-8")
    from xml.sax.saxutils import escape as x
    opf = ["<?xml version='1.0' encoding='utf-8'?>",
           '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">',
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">',
           f"<dc:title>{x(book.title)}</dc:title>",
           f'<dc:creator opf:role="aut">{x(book.author)}</dc:creator>']
    if book.year:
        opf.append(f"<dc:date>{book.year}-01-01T00:00:00+00:00</dc:date>")
    if book.language:
        opf.append(f"<dc:language>{x(book.language)}</dc:language>")
    if book.description:
        opf.append(f"<dc:description>{x(book.description)}</dc:description>")
    if book.series:
        opf.append(f'<meta name="calibre:series" content="{x(book.series, {chr(34): "&quot;"})}"/>')
        if book.series_index:
            opf.append(f'<meta name="calibre:series_index" content="{book.series_index}"/>')
    opf += ["</metadata>", "</package>"]
    (folder / "metadata.opf").write_text("\n".join(opf) + "\n", encoding="utf-8")
