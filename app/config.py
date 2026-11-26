import os
from pathlib import Path


def _b(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


TTS_URL = os.getenv("TTS_URL", "http://kokoro:8880").rstrip("/")
TTS_MODEL = os.getenv("TTS_MODEL", "kokoro")
TTS_CONCURRENCY = max(1, int(os.getenv("TTS_CONCURRENCY", "2")))
SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "24000"))  # Kokoro outputs 24 kHz

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/output"))
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))

MAX_CHARS = int(os.getenv("MAX_CHARS", "350"))
BITRATE = os.getenv("BITRATE", "64k")
ANNOUNCE_CHAPTERS = _b("ANNOUNCE_CHAPTERS", "1")
SENTENCE_PAUSE_MS = int(os.getenv("SENTENCE_PAUSE_MS", "150"))
PARAGRAPH_PAUSE_MS = int(os.getenv("PARAGRAPH_PAUSE_MS", "700"))

# Folders under OUTPUT_DIR. Fields: {author} {title} {series} {seq} {year}
# ({seq} renders as "Vol 2", the form Audiobookshelf reads). Segments that end up empty are
# dropped, as are empty "()" and dangling " - ". Books without a series skip the series folder.
OUTPUT_PATTERN = os.getenv("OUTPUT_PATTERN", "{author}/{series}/{title}")

# Credited as narrator in tags / reader.txt. {voice} is the selected voice.
NARRATOR = os.getenv("NARRATOR", "Kokoro AI ({voice})")

# If the target .m4b already exists: skip (default) or overwrite.
OVERWRITE = _b("OVERWRITE", "0")

# Optional: ask Audiobookshelf to scan after each finished book (useful when its library sits on a
# network mount where the folder watcher doesn't fire).
ABS_URL = os.getenv("ABS_URL", "").rstrip("/")
ABS_API_KEY = os.getenv("ABS_API_KEY", "")
ABS_LIBRARY_ID = os.getenv("ABS_LIBRARY_ID", "")

UMASK = int(os.getenv("UMASK", "002"), 8)

DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "af_heart")

CONVERT_TIMEOUT = int(os.getenv("CONVERT_TIMEOUT", "1200"))  # seconds, Calibre conversion
