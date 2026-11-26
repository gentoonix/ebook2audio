"""End-to-end smoke test: fake TTS + real app + real ffmpeg. Run from the project root:  python tests/smoke.py
CI runs this before publishing a rebuilt image, so a broken dependency update never ships."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_up(url: str) -> None:
    for _ in range(60):
        try:
            httpx.get(url, timeout=2)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise SystemExit(f"service did not start: {url}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    procs = []
    try:
        import make_epub
        epub = tmp / "book.epub"
        make_epub.make(str(epub), "Smoke Test", "Sam Ple", 3)

        tts_port, app_port = free_port(), free_port()
        env = {**os.environ, "TTS_URL": f"http://127.0.0.1:{tts_port}", "DATA_DIR": str(tmp / "data"),
               "OUTPUT_DIR": str(tmp / "out"), "CONFIG_DIR": str(tmp / "cfg"), "MOCK_DELAY": "0.15"}
        for mod, port in (("tests.mock_tts:app", tts_port), ("app.main:app", app_port)):
            procs.append(subprocess.Popen([sys.executable, "-W", "ignore", "-m", "uvicorn", mod, "--port", str(port)],
                                          cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        base = f"http://127.0.0.1:{app_port}"
        wait_up(f"{base}/api/health")

        epub2 = tmp / "book2.epub"
        make_epub.make(str(epub2), "Second Smoke", "Sam Ple", 1)
        with open(epub, "rb") as f1, open(epub2, "rb") as f2:
            r = httpx.post(f"{base}/api/jobs", files=[("files", ("book.epub", f1)), ("files", ("book2.epub", f2))],
                           data={"voice": "af_bella", "series": "Smoke Saga", "narrator": "Test Voice"})
        r.raise_for_status()
        ids = r.json()["created"]
        # edit the still-queued second job: series number + title, without touching the first
        meta = httpx.patch(f"{base}/api/jobs/{ids[1]}/meta", json={"series_index": "2", "title": "Second Smoke!"})
        assert meta.status_code == 200 and meta.json()["series"] == "Smoke Saga", meta.text

        for _ in range(180):
            jobs = {j["id"]: j for j in httpx.get(f"{base}/api/jobs").json()}
            if all(jobs[i]["status"] in ("done", "failed") for i in ids):
                break
            time.sleep(1)
        job = jobs[ids[0]]
        assert job["status"] == "done", job
        job2 = jobs[ids[1]]
        assert job2["status"] == "done", job2
        assert "Smoke Saga/Second Smoke!" in job2["output"], job2["output"]
        opf = (Path(job2["output"]).parent / "metadata.opf").read_text()
        assert 'calibre:series_index" content="2"' in opf, opf
        assert (Path(job2["output"]).parent / "reader.txt").read_text().strip() == "Test Voice"

        m4b = Path(job["output"])
        assert m4b.exists() and m4b.stat().st_size > 10_000, "m4b missing or tiny"
        assert (m4b.parent / "metadata.opf").exists() and (m4b.parent / "reader.txt").exists()
        probe = json.loads(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-of", "json", str(m4b)]))
        assert len(probe["chapters"]) == 3, probe["chapters"]
        assert probe["format"]["tags"]["artist"] == "Sam Ple"
        print("smoke test passed:", m4b.relative_to(tmp / "out"))
    finally:
        for p in procs:
            p.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
