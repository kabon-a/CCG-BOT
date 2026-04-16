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
