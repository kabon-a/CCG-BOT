import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set in environment or .env file")

_db_path_env = os.getenv("DATABASE_PATH", "").strip()
if _db_path_env:
    _candidate = Path(_db_path_env).expanduser()
    DATABASE_PATH = _candidate if _candidate.is_absolute() else (Path(__file__).parent / _candidate)
else:
    DATABASE_PATH = Path(__file__).parent / "data" / "ccg_elo.db"

DEFAULT_ELO = 1000
K_FACTOR = 32  # Standard ELO sensitivity

# Interspace integration — set both in Railway / .env on the bot host.
# INTERSPACE_URL: the Railway backend URL, e.g. https://review-system-production.up.railway.app
# INTERSPACE_BOT_SECRET: must match the BOT_SECRET env var on the Interspace backend.
INTERSPACE_URL = os.getenv("INTERSPACE_URL", "").rstrip("/")
INTERSPACE_BOT_SECRET = os.getenv("INTERSPACE_BOT_SECRET", "")

# PSCT report → Cursor cloud agent → PR
# CURSOR_API_KEY: user or service-account key from Cursor Dashboard → API Keys
# REPORT_CHANNEL_ID: Discord channel snowflake for #report-a-problem (optional; name fallback used)
CURSOR_API_KEY = os.getenv("CURSOR_API_KEY", "").strip()
CURSOR_API_BASE = os.getenv("CURSOR_API_BASE", "https://api.cursor.com").rstrip("/")
CURSOR_MODEL = os.getenv("CURSOR_MODEL", "composer-2.5").strip() or "composer-2.5"
PSCT_REPO_URL = os.getenv(
    "PSCT_REPO_URL",
    "https://github.com/kabon-a/ccg-interspace",
).strip().rstrip("/")
PSCT_REPO_REF = os.getenv("PSCT_REPO_REF", "main").strip() or "main"
_report_channel_raw = os.getenv("REPORT_CHANNEL_ID", "").strip()
REPORT_CHANNEL_ID = int(_report_channel_raw) if _report_channel_raw.isdigit() else None
REPORT_CHANNEL_NAME = os.getenv("REPORT_CHANNEL_NAME", "report-a-problem").strip() or "report-a-problem"
