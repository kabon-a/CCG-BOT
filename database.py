"""Database and ELO logic for CCG leaderboards (members + archetype tier list)."""

import json
import re
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


