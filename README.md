# ebook2audio

Upload ebooks in a browser, pick a voice, and get chaptered `.m4b` audiobooks in a folder
that Audiobookshelf (and FileBrowser Quantum) can see. Multiple books queue up and run one at a time.
TTS is Kokoro-82M via Kokoro-FastAPI (OpenAI-compatible), so the same app works on CPU or an NVIDIA GPU.

## Run

The app image is published to GitHub Container Registry by the included workflow (see *Updates* below), so the
compose file pulls it instead of building locally. Replace `YOUR_GITHUB_USER` in `docker-compose.yml` (or set `APP_IMAGE`).

```bash
# CPU only (works today on a box with no GPU; slow but fine overnight)
OUTPUT_HOST_PATH=/path/to/audiobooks docker compose --profile cpu up -d

# NVIDIA GPU (needs the NVIDIA container toolkit / TrueNAS GPU passthrough)
OUTPUT_HOST_PATH=/path/to/audiobooks docker compose --profile gpu up -d

```

Open `http://<host>:8000`. Without a registry you can still build locally: `docker build -t ebook2audio .` and set `APP_IMAGE=ebook2audio`.

## TrueNAS SCALE
The ready-to-paste file is `deploy/truenas-gpu.yml` (NVIDIA).
Edit the marked lines (GitHub user, dataset paths), then Apps → Discover Apps → ⋮ → **Install via YAML**.

## Updates

Nothing is applied automatically; you decide when.

| Component | How new versions reach you |
|---|---|
| **This app + Calibre + ffmpeg + Python libraries** | `.github/workflows/build.yml` rebuilds the image every Monday with no cache, so new releases land in `ghcr.io/<you>/ebook2audio:latest` |
| **Kokoro TTS server** | Upstream publishes to `kokoro-fastapi-gpu:latest` |

**Safety net:** before publishing, the workflow runs `tests/smoke.py` (fake TTS, real app and ffmpeg, checks chapters and tags).
If a dependency update breaks the app, the build fails and `:latest` keeps pointing at the last good image. Each published image also gets a
numbered tag, so you can pin an older one. The test can't cover Kokoro itself, so read its release notes before updating it.

**Applying an update:** both services use `pull_policy: always`, so redeploying the app pulls the newest images. TrueNAS can flag available
image updates for apps in Apps → Configuration → Settings. I haven't confirmed that a YAML app's Edit → Save redeploy re-pulls on TrueNAS; if it doesn't,
use the app's Stop/Start or pull the image from a shell. Jobs resume after a restart, so updating mid-book only repeats a few sentences.

### Cloudflare Tunnel
Point the tunnel at `http://<host>:8000` and put **Cloudflare Access** in front of it.
The app has no login of its own, and anyone who reaches it can queue jobs.

### Output location / FileBrowser Quantum
`OUTPUT_HOST_PATH` is a normal folder on the processing server. Add that same folder as a source in
FileBrowser Quantum and it can browse and share it. If Audiobookshelf runs on a different machine,
make this folder an NFS/SMB share (or mount the destination server's library here) so the finished
files land where ABS scans.

### Audiobookshelf / Chaptarr compatibility
Each book gets its own folder, which is what Audiobookshelf (ABS) requires. Default layout:
`Author/Series/Title/Title.m4b` (books without a series skip the series folder). Inside the folder:

| File | Purpose |
|---|---|
| `Title.m4b` | AAC audio with chapter markers; tags: title, author (artist + album artist), album, narrator (composer), description, genre, year |
| `cover.png` / `cover.jpg` | cover from the EPUB |
| `metadata.opf` | title, author, year, language, description, **series and series number** (ABS reads these from OPF) |
| `reader.txt`, `desc.txt` | narrator and description (ABS reads both). Narrator defaults to `Kokoro AI (<voice>)`; change with `NARRATOR` |

Series name and number come from the EPUB's Calibre metadata. Books without it land outside a series folder.
MP4 tags can't carry series info (ffmpeg doesn't write it), so the OPF is what carries it.

`OUTPUT_PATTERN` tokens: `{author} {series} {seq} {title} {year}`. `{seq}` renders as `Vol 2`, which ABS recognises.
Empty parts are dropped. Examples:

| Pattern | Result |
|---|---|
| `{author}/{series}/{title}` | `Terry Goodkind/Sword of Truth/Wizard's First Rule/` (default) |
| `{author}/{series}/{seq} - {year} - {title}` | `.../Sword of Truth/Vol 1 - 1994 - Wizard's First Rule/` |
| `{author}/{title}` | flat, no series folders |

**Chaptarr:** I couldn't find Chaptarr's naming tokens or import rules in its public docs (its wiki wasn't reachable), so
check the audiobook naming format in Chaptarr and match it with `OUTPUT_PATTERN` if you want it to recognise these files
as-is. Chaptarr says it matches files by tags, folders and release metadata, and this app writes all three.
If Chaptarr also renames or moves files in a library, point this app at a separate folder first and
import from there rather than sharing a root.

**Already-existing books are skipped** (shown as "Skipped: already in library"), so a duplicate upload can't
overwrite a finished book. Set `OVERWRITE=1` to replace instead.

**Permissions:** the container runs as `PUID:PGID` (default 568, the TrueNAS `apps` user) with umask 002. Set them to the
owner that ABS/Chaptarr use on the shared folder, and make sure `data`, `config` and the output folder are writable by that user.

**Instant library updates:** ABS's folder watcher often doesn't fire on network mounts. To have this app trigger a scan
after each book, set `ABS_URL`, `ABS_API_KEY` (ABS Settings → API Keys) and `ABS_LIBRARY_ID`. I wrote this against ABS's
`POST /api/libraries/<id>/scan` endpoint and tested it only against a stand-in server.

### Metadata without editing every book
- **Automatic:** title, author, series and number (when the EPUB carries Calibre series metadata), year, language and
  description are read from each file and written to the tags and sidecar files.
- **Upload-wide:** the optional "Metadata for this upload" box sets series, author or narrator once for every book in that upload.
  Handy for a whole series. Leave it empty for books that already carry their own metadata.
- **Per book (only when needed):** an **Edit** button on waiting, failed or cancelled jobs changes title, author, series, number and
  narrator before processing. The job list shows what was detected. Books converted from non-EPUB formats usually have no series
  info, so use these two for them.
- **Bulk fixes later:** Audiobookshelf has its own bulk match against online providers. That fills in series and other details for
  books already in your library, so you don't have to edit them here. I haven't verified how its priority rules interact with the sidecar files.

### Pronunciation fixes
Create `config/pronunciation.txt`, one rule per line (`#` for comments):

```
NASA=nassa
Hermione=her-my-oh-nee
```
Whole-word, case-sensitive replacements applied before TTS.

### Settings (env vars on the `app` service)
`TTS_CONCURRENCY` (parallel requests; 1 for weak CPUs, 2-4 for GPU), `BITRATE` (default 64k),
`MAX_CHARS` (chunk size, default 350), `ANNOUNCE_CHAPTERS` (reads chapter titles aloud, default on),
`SENTENCE_PAUSE_MS`, `PARAGRAPH_PAUSE_MS`, `DEFAULT_VOICE`, `CONVERT_TIMEOUT` (seconds allowed for Calibre conversion, default 1200).

### Voices
The dropdown is filled from the Kokoro server. To blend, type e.g. `af_bella+af_sky` in the custom box.
Use "Preview voice" before queuing a book.

## Behavior notes
- Accepts EPUB, MOBI, AZW/AZW3, FB2, PDF, DOCX, RTF, ODT, HTML and TXT. Non-EPUB files are converted automatically with Calibre (`ebook-convert`), which adds roughly half a gigabyte to the image. DRM-protected files can't be converted.
- PDFs and TXT files have no real chapter structure, so chapter splits are best-effort. Page headers, footers and page numbers in PDFs may get read aloud. EPUB gives the best result.
- Jobs are resumable: finished chapters are cached in `data/work`, so a restart or crash continues where it left off.
- Cancel / Retry / Remove buttons per job.
