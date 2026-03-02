"""Database and ELO logic for CCG leaderboards."""

import aiosqlite
from config import DATABASE_PATH, DEFAULT_ELO, K_FACTOR


def elo_expected(score_a: float, score_b: float) -> float:
    """Expected score for player A vs player B."""
    return 1 / (1 + 10 ** ((score_b - score_a) / 400))


def elo_change(winner_elo: float, loser_elo: float) -> tuple[float, float]:
    """Return (winner_delta, loser_delta) for ELO update."""
    expected_winner = elo_expected(winner_elo, loser_elo)
    delta = K_FACTOR * (1 - expected_winner)
    return (delta, -delta)


async def init_db() -> None:
    """Create database and tables."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS leaderboards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                UNIQUE(guild_id, name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                leaderboard_id INTEGER NOT NULL,
                card_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                elo REAL NOT NULL DEFAULT ?,
                FOREIGN KEY (leaderboard_id) REFERENCES leaderboards(id) ON DELETE CASCADE,
                UNIQUE(leaderboard_id, LOWER(card_name))
            )
        """, (DEFAULT_ELO,))
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_entries_leaderboard
            ON entries(leaderboard_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_leaderboards_guild
            ON leaderboards(guild_id)
        """)
        await db.commit()


async def create_leaderboard(guild_id: int, name: str) -> int | None:
    """Create a new leaderboard. Returns id or None if name already exists."""
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cur = await db.execute(
                "INSERT INTO leaderboards (guild_id, name) VALUES (?, ?) RETURNING id",
                (guild_id, name.strip())
            )
            row = await cur.fetchone()
            await db.commit()
            return row[0] if row else None
    except aiosqlite.IntegrityError:
        return None


async def list_leaderboards(guild_id: int) -> list[tuple[int, str]]:
    """Return list of (id, name) for guild."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, name FROM leaderboards WHERE guild_id = ? ORDER BY name",
            (guild_id,)
        )
        rows = await cur.fetchall()
        return [(r["id"], r["name"]) for r in rows]


async def get_leaderboard_id(guild_id: int, name: str) -> int | None:
    """Get leaderboard id by guild and name (case-insensitive)."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "SELECT id FROM leaderboards WHERE guild_id = ? AND LOWER(name) = LOWER(?)",
            (guild_id, name.strip())
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def add_entry(leaderboard_id: int, card_name: str, display_name: str | None = None) -> bool:
    """Add a card to the leaderboard. Returns False if already exists."""
    name = card_name.strip()
    display = (display_name or name).strip()
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "INSERT INTO entries (leaderboard_id, card_name, display_name, elo) VALUES (?, ?, ?, ?)",
                (leaderboard_id, name, display, DEFAULT_ELO)
            )
            await db.commit()
            return True
    except aiosqlite.IntegrityError:
        return False


async def get_entry(leaderboard_id: int, card_name: str) -> tuple[int, float, str] | None:
    """Return (id, elo, display_name) or None."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, elo, display_name FROM entries WHERE leaderboard_id = ? AND LOWER(card_name) = LOWER(?)",
            (leaderboard_id, card_name.strip())
        )
        row = await cur.fetchone()
        return (row["id"], row["elo"], row["display_name"]) if row else None


async def set_display_name(leaderboard_id: int, card_name: str, new_display_name: str) -> bool:
    """Update display name for an entry. Returns True if updated."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "UPDATE entries SET display_name = ? WHERE leaderboard_id = ? AND LOWER(card_name) = LOWER(?)",
            (new_display_name.strip(), leaderboard_id, card_name.strip())
        )
        await db.commit()
        return cur.rowcount > 0


async def record_match(leaderboard_id: int, winner_card: str, loser_card: str) -> bool:
    """Record a match result and update ELO. Returns False if either card not found."""
    winner = await get_entry(leaderboard_id, winner_card)
    loser = await get_entry(leaderboard_id, loser_card)
    if not winner or not loser:
        return False

    wid, welo, _ = winner
    lid, lelo, _ = loser
    wdelta, ldelta = elo_change(welo, lelo)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE entries SET elo = elo + ? WHERE id = ?", (wdelta, wid))
        await db.execute("UPDATE entries SET elo = elo + ? WHERE id = ?", (ldelta, lid))
        await db.commit()

    return True


async def get_leaderboard(leaderboard_id: int, limit: int = 25) -> list[tuple[str, str, float]]:
    """Return list of (card_name, display_name, elo) sorted by ELO descending."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "SELECT card_name, display_name, elo FROM entries WHERE leaderboard_id = ? ORDER BY elo DESC LIMIT ?",
            (leaderboard_id, limit)
        )
        return await cur.fetchall()


async def remove_entry(leaderboard_id: int, card_name: str) -> bool:
    """Remove an entry. Returns True if removed."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "DELETE FROM entries WHERE leaderboard_id = ? AND LOWER(card_name) = LOWER(?)",
            (leaderboard_id, card_name.strip())
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_leaderboard(guild_id: int, name: str) -> bool:
    """Delete a leaderboard. Returns True if deleted."""
    lb_id = await get_leaderboard_id(guild_id, name)
    if not lb_id:
        return False
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM leaderboards WHERE id = ?", (lb_id,))
        await db.commit()
    return True
