"""Database and ELO logic for CCG leaderboards (members + archetype tier list)."""

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

import aiosqlite

from config import DATABASE_PATH


def normalize_deck_name(name: str) -> str:
    """Normalize deck/archetype name to avoid duplicate entries (case, spacing, punctuation)."""
    s = name.strip().lower()
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[-_./]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


@dataclass
class EloSettings:
    """ELO configuration for a leaderboard."""

    default_rating: float = 1000
    k_factor: float = 32
    precision: int = 0
    loss_dampen: float = 1.0
    max_advantage: float = 32
    curve_factor: float = 400
    influence_range: float = 400
    ffa_distribution: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_rating": self.default_rating,
            "k_factor": self.k_factor,
            "precision": self.precision,
            "loss_dampen": self.loss_dampen,
            "max_advantage": self.max_advantage,
            "curve_factor": self.curve_factor,
            "influence_range": self.influence_range,
            "ffa_distribution": self.ffa_distribution,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EloSettings":
        return cls(
            default_rating=float(d.get("default_rating", 1000)),
            k_factor=float(d.get("k_factor", 32)),
            precision=int(d.get("precision", 0)),
            loss_dampen=float(d.get("loss_dampen", 1.0)),
            max_advantage=float(d.get("max_advantage", 32)),
            curve_factor=float(d.get("curve_factor", 400)),
            influence_range=float(d.get("influence_range", 400)),
            ffa_distribution=float(d.get("ffa_distribution", 1.0)),
        )


def elo_expected(score_a: float, score_b: float, curve_factor: float = 400) -> float:
    """Expected score for player A vs player B."""
    return 1 / (1 + 10 ** ((score_b - score_a) / curve_factor))


def elo_change(
    winner_elo: float,
    loser_elo: float,
    settings: EloSettings,
) -> tuple[float, float]:
    """Return (winner_delta, loser_delta) for ELO update."""
    expected_winner = elo_expected(winner_elo, loser_elo, settings.curve_factor)
    raw_delta = settings.k_factor * (1 - expected_winner)
    raw_delta = max(-settings.max_advantage, min(settings.max_advantage, raw_delta))
    winner_delta = raw_delta
    loser_delta = -raw_delta * settings.loss_dampen
    return (winner_delta, loser_delta)


def format_elo(elo: float, precision: int) -> str:
    if precision <= 0:
        return str(int(round(elo)))
    return f"{elo:.{precision}f}"


async def init_db() -> None:
    """Create database and tables."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        # Leaderboards with ELO settings (JSON)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS leaderboards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                elo_settings TEXT NOT NULL DEFAULT '{}',
                UNIQUE(guild_id, name)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS member_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                leaderboard_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT,
                elo REAL NOT NULL,
                FOREIGN KEY (leaderboard_id) REFERENCES leaderboards(id) ON DELETE CASCADE,
                UNIQUE(leaderboard_id, user_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS archetypes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                canonical_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                elo REAL NOT NULL DEFAULT 1000,
                UNIQUE(guild_id, canonical_name)
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_member_entries_leaderboard ON member_entries(leaderboard_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_archetypes_guild ON archetypes(guild_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_leaderboards_guild ON leaderboards(guild_id)
        """)

        # Active users (for @active role, last_activity within 7 days)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_users (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                last_activity REAL NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_active_users_guild ON active_users(guild_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_active_users_activity ON active_users(last_activity)")

        # Polls (custom polls with reactions, not Discord native)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                options TEXT NOT NULL,
                role_ids TEXT NOT NULL,
                ends_at REAL NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS poll_votes (
                poll_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                option_index INTEGER NOT NULL,
                PRIMARY KEY (poll_id, user_id, option_index),
                FOREIGN KEY (poll_id) REFERENCES polls(id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_polls_ends_at ON polls(ends_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_polls_guild ON polls(guild_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_poll_votes_poll ON poll_votes(poll_id)")

        # Migration: drop old entries table (legacy card-based schema)
        await db.execute("DROP TABLE IF EXISTS entries")

        # Migration: add elo_settings to existing leaderboards
        try:
            await db.execute("ALTER TABLE leaderboards ADD COLUMN elo_settings TEXT DEFAULT '{}'")
        except aiosqlite.OperationalError:
            pass  # Column already exists

        await db.commit()


def _default_settings_json() -> str:
    return json.dumps(EloSettings().to_dict())


async def create_leaderboard(guild_id: int, name: str) -> int | None:
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cur = await db.execute(
                "INSERT INTO leaderboards (guild_id, name, elo_settings) VALUES (?, ?, ?) RETURNING id",
                (guild_id, name.strip(), _default_settings_json()),
            )
            row = await cur.fetchone()
            await db.commit()
            return row[0] if row else None
    except aiosqlite.IntegrityError:
        return None


async def list_leaderboards(guild_id: int) -> list[tuple[int, str]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, name FROM leaderboards WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )
        rows = await cur.fetchall()
        return [(r["id"], r["name"]) for r in rows]


async def get_leaderboard_id(guild_id: int, name: str) -> int | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "SELECT id FROM leaderboards WHERE guild_id = ? AND LOWER(name) = LOWER(?)",
            (guild_id, name.strip()),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def get_leaderboard_settings(leaderboard_id: int) -> EloSettings:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "SELECT elo_settings FROM leaderboards WHERE id = ?",
            (leaderboard_id,),
        )
        row = await cur.fetchone()
        if row and row[0]:
            return EloSettings.from_dict(json.loads(row[0]))
        return EloSettings()


async def set_leaderboard_settings(leaderboard_id: int, settings: EloSettings) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "UPDATE leaderboards SET elo_settings = ? WHERE id = ?",
            (json.dumps(settings.to_dict()), leaderboard_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def add_member(leaderboard_id: int, user_id: int, display_name: str | None = None) -> bool:
    settings = await get_leaderboard_settings(leaderboard_id)
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "INSERT INTO member_entries (leaderboard_id, user_id, display_name, elo) VALUES (?, ?, ?, ?)",
                (leaderboard_id, user_id, display_name, settings.default_rating),
            )
            await db.commit()
            return True
    except aiosqlite.IntegrityError:
        return False


async def get_member_entry(leaderboard_id: int, user_id: int) -> tuple[int, float, str | None] | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, elo, display_name FROM member_entries WHERE leaderboard_id = ? AND user_id = ?",
            (leaderboard_id, user_id),
        )
        row = await cur.fetchone()
        return (row["id"], row["elo"], row["display_name"]) if row else None


async def get_or_create_archetype(guild_id: int, deck_name: str) -> tuple[int, float, str]:
    """Get or create archetype. Returns (id, elo, display_name)."""
    canonical = normalize_deck_name(deck_name)
    display = deck_name.strip()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, elo, display_name FROM archetypes WHERE guild_id = ? AND canonical_name = ?",
            (guild_id, canonical),
        )
        row = await cur.fetchone()
        if row:
            return (row["id"], row["elo"], row["display_name"])
        await db.execute(
            "INSERT INTO archetypes (guild_id, canonical_name, display_name, elo) VALUES (?, ?, ?, 1000)",
            (guild_id, canonical, display),
        )
        await db.commit()
        cur = await db.execute(
            "SELECT id, elo, display_name FROM archetypes WHERE guild_id = ? AND canonical_name = ?",
            (guild_id, canonical),
        )
        r = await cur.fetchone()
        return (r["id"], r["elo"], r["display_name"])


async def record_match(
    guild_id: int,
    leaderboard_id: int,
    winner_user_id: int,
    loser_user_id: int,
    winner_deck: str,
    loser_deck: str,
) -> bool:
    """Record match and update both member ELO and archetype ELO."""
    winner_mem = await get_member_entry(leaderboard_id, winner_user_id)
    loser_mem = await get_member_entry(leaderboard_id, loser_user_id)
    if not winner_mem or not loser_mem:
        return False

    settings = await get_leaderboard_settings(leaderboard_id)

    wid, welo, _ = winner_mem
    lid, lelo, _ = loser_mem
    wdelta, ldelta = elo_change(welo, lelo, settings)

    # Archetype ELO (use default 1000/32/400 for tier list)
    tier_settings = EloSettings(default_rating=1000, k_factor=32)
    _, winner_arch_elo, _ = await get_or_create_archetype(guild_id, winner_deck)
    _, loser_arch_elo, _ = await get_or_create_archetype(guild_id, loser_deck)
    arch_wdelta, arch_ldelta = elo_change(winner_arch_elo, loser_arch_elo, tier_settings)

    winner_canon = normalize_deck_name(winner_deck)
    loser_canon = normalize_deck_name(loser_deck)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE member_entries SET elo = elo + ? WHERE id = ?", (wdelta, wid))
        await db.execute("UPDATE member_entries SET elo = elo + ? WHERE id = ?", (ldelta, lid))
        await db.execute(
            "UPDATE archetypes SET elo = elo + ? WHERE guild_id = ? AND canonical_name = ?",
            (arch_wdelta, guild_id, winner_canon),
        )
        await db.execute(
            "UPDATE archetypes SET elo = elo + ? WHERE guild_id = ? AND canonical_name = ?",
            (arch_ldelta, guild_id, loser_canon),
        )
        await db.commit()

    return True


async def get_member_leaderboard(leaderboard_id: int, limit: int = 25) -> list[tuple[int, str | None, float]]:
    """Return list of (user_id, display_name, elo) sorted by ELO descending."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, display_name, elo FROM member_entries WHERE leaderboard_id = ? ORDER BY elo DESC LIMIT ?",
            (leaderboard_id, limit),
        )
        return [(r[0], r[1], r[2]) for r in await cur.fetchall()]


async def get_tier_list(guild_id: int, limit: int = 25) -> list[tuple[str, str, float]]:
    """Return list of (canonical_name, display_name, elo) sorted by ELO descending."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "SELECT canonical_name, display_name, elo FROM archetypes WHERE guild_id = ? ORDER BY elo DESC LIMIT ?",
            (guild_id, limit),
        )
        return await cur.fetchall()


async def set_member_display_name(leaderboard_id: int, user_id: int, display_name: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "UPDATE member_entries SET display_name = ? WHERE leaderboard_id = ? AND user_id = ?",
            (display_name.strip(), leaderboard_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def remove_member(leaderboard_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "DELETE FROM member_entries WHERE leaderboard_id = ? AND user_id = ?",
            (leaderboard_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def reset_leaderboard(guild_id: int, name: str) -> bool:
    """Reset all member ELOs on a leaderboard to default. Returns True if reset."""
    lb_id = await get_leaderboard_id(guild_id, name)
    if not lb_id:
        return False
    settings = await get_leaderboard_settings(lb_id)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE member_entries SET elo = ? WHERE leaderboard_id = ?",
            (settings.default_rating, lb_id),
        )
        await db.commit()
    return True


async def reset_tier_list(guild_id: int) -> bool:
    """Reset all archetype ELOs to 1000."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("UPDATE archetypes SET elo = 1000 WHERE guild_id = ?", (guild_id,))
        await db.commit()
        return cur.rowcount >= 0


async def delete_leaderboard(guild_id: int, name: str) -> bool:
    lb_id = await get_leaderboard_id(guild_id, name)
    if not lb_id:
        return False
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM leaderboards WHERE id = ?", (lb_id,))
        await db.commit()
    return True


# --- Active users (for @active role) ---

ACTIVE_WINDOW_SECONDS = 7 * 24 * 60 * 60  # 7 days


async def record_activity(guild_id: int, user_id: int) -> None:
    """Record or update last activity for a user in a guild."""
    now = time.time()
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO active_users (guild_id, user_id, last_activity) VALUES (?, ?, ?)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET last_activity = excluded.last_activity
            """,
            (guild_id, user_id, now),
        )
        await conn.commit()


async def get_active_user_ids(guild_id: int) -> set[int]:
    """Return user IDs with activity within the last 7 days."""
    cutoff = time.time() - ACTIVE_WINDOW_SECONDS
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute(
            "SELECT user_id FROM active_users WHERE guild_id = ? AND last_activity >= ?",
            (guild_id, cutoff),
        )
        return {r[0] for r in await cur.fetchall()}


async def get_user_ids_to_remove_active(guild_id: int) -> list[int]:
    """Return user IDs whose last activity is older than 7 days (for role cleanup)."""
    cutoff = time.time() - ACTIVE_WINDOW_SECONDS
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute(
            "SELECT user_id FROM active_users WHERE guild_id = ? AND last_activity < ?",
            (guild_id, cutoff),
        )
        return [r[0] for r in await cur.fetchall()]


# --- Polls ---

async def create_poll(
    guild_id: int,
    channel_id: int,
    message_id: int,
    title: str,
    options: list[str],
    role_ids: list[int],
    duration_seconds: int,
) -> int:
    """Create a poll. Returns poll ID."""
    now = time.time()
    ends_at = now + duration_seconds
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute(
            """
            INSERT INTO polls (guild_id, channel_id, message_id, title, options, role_ids, ends_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                message_id,
                title,
                json.dumps(options),
                json.dumps(role_ids),
                ends_at,
                now,
            ),
        )
        await conn.commit()
        return cur.lastrowid


async def get_poll_by_message(guild_id: int, message_id: int) -> dict | None:
    """Get poll by guild and message ID."""
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM polls WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return dict(row)


async def get_poll_by_id(poll_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM polls WHERE id = ?", (poll_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_pending_polls() -> list[dict]:
    """Get all polls that have ended but may need closing."""
    now = time.time()
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM polls WHERE ends_at <= ?", (now,))
        return [dict(r) for r in await cur.fetchall()]


async def add_poll_vote(poll_id: int, user_id: int, option_index: int) -> bool:
    """Record a vote. Returns True if added, False if already voted for that option."""
    try:
        async with aiosqlite.connect(DATABASE_PATH) as conn:
            await conn.execute(
                "INSERT INTO poll_votes (poll_id, user_id, option_index) VALUES (?, ?, ?)",
                (poll_id, user_id, option_index),
            )
            await conn.commit()
            return True
    except aiosqlite.IntegrityError:
        return False


async def remove_poll_vote(poll_id: int, user_id: int, option_index: int) -> bool:
    """Remove a vote."""
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute(
            "DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ? AND option_index = ?",
            (poll_id, user_id, option_index),
        )
        await conn.commit()
        return cur.rowcount > 0


async def get_poll_votes(poll_id: int) -> list[tuple[int, int]]:
    """Return list of (user_id, option_index) for all votes in the poll."""
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute(
            "SELECT user_id, option_index FROM poll_votes WHERE poll_id = ?",
            (poll_id,),
        )
        return [(r[0], r[1]) for r in await cur.fetchall()]


async def delete_poll(poll_id: int) -> None:
    """Delete a poll and its votes."""
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute("DELETE FROM polls WHERE id = ?", (poll_id,))
        await conn.commit()


