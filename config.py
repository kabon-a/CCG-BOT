import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set in environment or .env file")

DATABASE_PATH = Path(__file__).parent / "data" / "ccg_elo.db"
DEFAULT_ELO = 1000
K_FACTOR = 32  # Standard ELO sensitivity
