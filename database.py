"""Database and ELO logic for CCG leaderboards (members + archetype tier list)."""

import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
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
    """ELO configuration for a leaderboard (YCCG / Team Up–style fields)."""

    # Core
    default_rating: float = 1000
    k_factor: float = 32
    precision: int = 0
    loss_dampen: float = 1.0
    matches_required_for_ranking: int = 0
    inactive_days_threshold: int = 0
    locked: bool = False

    # Rating difference (logistic); base is typically 10, influence_range is the denominator (often 400)
    max_advantage: float = 0.0  # 0 = no cap on per-match delta magnitude
    curve_factor: float = 10.0  # base of exponent (standard ELO uses 10)
    influence_range: float = 400.0
    ffa_distribution: float = 1.0

    # Capping (clamp effective rating gap for expectation); cap_range 0 = use influence_range
    cap_range: float = 0.0
    cap_favorite_win_impact: bool = True
    cap_favorite_loss_impact: bool = True
    cap_underdog_win_impact: bool = True
    cap_underdog_loss_impact: bool = True

    # Display (optional)
    display_title: str | None = None
    description: str | None = None
    icon_url: str | None = None
    banner_url: str | None = None
    primary_color: int | None = None
    secondary_color: int | None = None

    # Internal migration marker (not user-facing)
    _migrated_curve_v2: bool = field(default=True, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_rating": self.default_rating,
            "k_factor": self.k_factor,
            "precision": self.precision,
            "loss_dampen": self.loss_dampen,
            "matches_required_for_ranking": self.matches_required_for_ranking,
            "inactive_days_threshold": self.inactive_days_threshold,
            "locked": self.locked,
            "max_advantage": self.max_advantage,
            "curve_factor": self.curve_factor,
            "influence_range": self.influence_range,
            "ffa_distribution": self.ffa_distribution,
            "cap_range": self.cap_range,
            "cap_favorite_win_impact": self.cap_favorite_win_impact,
            "cap_favorite_loss_impact": self.cap_favorite_loss_impact,
            "cap_underdog_win_impact": self.cap_underdog_win_impact,
            "cap_underdog_loss_impact": self.cap_underdog_loss_impact,
            "display_title": self.display_title,
            "description": self.description,
            "icon_url": self.icon_url,
            "banner_url": self.banner_url,
            "primary_color": self.primary_color,
            "secondary_color": self.secondary_color,
            "_migrated_curve_v2": self._migrated_curve_v2,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EloSettings":
        curve_factor = float(d.get("curve_factor", 10))
        influence_range = float(d.get("influence_range", 400))
        # Legacy: only one "scale" existed; it was the denominator in 10 ** (Δ / scale), stored as curve_factor (~400).
        if curve_factor >= 60 and not d.get("_migrated_curve_v2", False):
            influence_range = curve_factor
            curve_factor = 10.0

        def _b(key: str, default: bool) -> bool:
            v = d.get(key, default)
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(v)
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes", "on")
            return default

        def _opt_int_color(v: Any) -> int | None:
            if v is None:
                return None
            return int(v)

        pc = d.get("primary_color")
        sc = d.get("secondary_color")

        return cls(
            default_rating=float(d.get("default_rating", 1000)),
            k_factor=float(d.get("k_factor", 32)),
            precision=int(d.get("precision", 0)),
            loss_dampen=float(d.get("loss_dampen", 1.0)),
            matches_required_for_ranking=int(d.get("matches_required_for_ranking", 0)),
            inactive_days_threshold=int(d.get("inactive_days_threshold", 0)),
            locked=_b("locked", False),
            max_advantage=float(d.get("max_advantage", 0)),
            curve_factor=curve_factor,
            influence_range=influence_range,
            ffa_distribution=float(d.get("ffa_distribution", 1.0)),
            cap_range=float(d.get("cap_range", 0)),
            cap_favorite_win_impact=_b("cap_favorite_win_impact", True),
            cap_favorite_loss_impact=_b("cap_favorite_loss_impact", True),
            cap_underdog_win_impact=_b("cap_underdog_win_impact", True),
            cap_underdog_loss_impact=_b("cap_underdog_loss_impact", True),
            display_title=d.get("display_title"),
            description=d.get("description"),
            icon_url=d.get("icon_url"),
            banner_url=d.get("banner_url"),
            primary_color=_opt_int_color(pc),
            secondary_color=_opt_int_color(sc),
            _migrated_curve_v2=True,
        )


def elo_change(
    winner_elo: float,
    loser_elo: float,
    settings: EloSettings,
) -> tuple[float, float]:
    """Return (winner_delta, loser_delta) for ELO update."""
    diff = loser_elo - winner_elo  # for A=winner, B=loser
    cap_lim = settings.cap_range if settings.cap_range > 0 else settings.influence_range

    winner_is_favorite = winner_elo > loser_elo

    if abs(diff) > cap_lim > 0:
        if winner_is_favorite:
            # Higher-rated player won: both "favorite win" and "underdog loss" describe this outcome.
            apply_clamp = settings.cap_favorite_win_impact and settings.cap_underdog_loss_impact
        elif winner_elo < loser_elo:
            # Upset: both "underdog win" and "favorite loss" describe this outcome.
            apply_clamp = settings.cap_underdog_win_impact and settings.cap_favorite_loss_impact
        else:
            apply_clamp = True
        if apply_clamp:
            diff = math.copysign(cap_lim, diff)

    base = max(settings.curve_factor, 1e-9)
    denom = max(settings.influence_range, 1e-9)
    expected_winner = 1 / (1 + base ** (diff / denom))

    raw_delta = settings.k_factor * (1 - expected_winner)
    ma = settings.max_advantage
    if ma > 0:
        raw_delta = max(-ma, min(ma, raw_delta))

    winner_delta = raw_delta
    loser_delta = -raw_delta * settings.loss_dampen
    return (winner_delta, loser_delta)


def format_elo(elo: float, precision: int) -> str:
    if precision <= 0:
        return str(int(round(elo)))
    return f"{elo:.{precision}f}"


@dataclass
class AaEloSettings:
    """Guild-wide Adjusted Archetype ELO (1v1 only)."""

    k_arch: float = 20.0
    n0: float = 10.0
    influence_range: float = 400.0
    min_games_display: int = 5

    def to_dict(self) -> dict[str, Any]:
        return {
            "k_arch": self.k_arch,
            "n0": self.n0,
            "influence_range": self.influence_range,
            "min_games_display": self.min_games_display,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AaEloSettings":
        return cls(
            k_arch=float(d.get("k_arch", 20)),
            n0=float(d.get("n0", 10)),
            influence_range=float(d.get("influence_range", 400)),
            min_games_display=int(d.get("min_games_display", 5)),
        )


def aa_elo_expected(r_a: float, r_b: float, influence: float) -> float:
    """Expected score for side A (win probability) vs B; AA-ELO / standard logistic with given denominator."""
    d = max(float(influence), 1e-9)
    return 1.0 / (1.0 + 10 ** ((r_b - r_a) / d))


def _ordered_archetype_pair(c1: str, c2: str) -> tuple[str, str]:
    return (c1, c2) if c1 < c2 else (c2, c1)


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

        # Live-updating leaderboard / tier list messages (channel embeds the bot edits)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS live_leaderboard_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                leaderboard_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                UNIQUE(guild_id, leaderboard_id, channel_id, kind),
                CHECK (kind IN ('rankings', 'tierlist'))
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_live_lb_guild ON live_leaderboard_messages(guild_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_live_lb_lookup ON live_leaderboard_messages(guild_id, leaderboard_id, kind)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS archetype_pair_counts (
                guild_id INTEGER NOT NULL,
                canon_a TEXT NOT NULL,
                canon_b TEXT NOT NULL,
                n INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, canon_a, canon_b)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_archetype_pairs_guild ON archetype_pair_counts(guild_id)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_undo_stack (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                actor_id INTEGER NOT NULL,
                payload TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_undo_stack_guild ON guild_undo_stack(guild_id, id)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_aa_elo (
                guild_id INTEGER PRIMARY KEY,
                k_arch REAL NOT NULL DEFAULT 20,
                n0 REAL NOT NULL DEFAULT 10,
                influence_range REAL NOT NULL DEFAULT 400,
                min_games_display INTEGER NOT NULL DEFAULT 5
            )
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

        # Two-stage tier polls (Simpson + EV fallback)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stage_polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                options TEXT NOT NULL,
                role_ids TEXT NOT NULL,
                num_tiers INTEGER NOT NULL,
                ends_at REAL NOT NULL,
                created_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'stage1_open',
                preference_options TEXT,
                preference_duration_seconds INTEGER NOT NULL DEFAULT 0,
                preference_ends_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stage_poll_votes (
                poll_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                option_index INTEGER NOT NULL,
                tier INTEGER NOT NULL,
                PRIMARY KEY (poll_id, user_id, option_index),
                FOREIGN KEY (poll_id) REFERENCES stage_polls(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stage_poll_pref_votes (
                poll_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                option_index INTEGER NOT NULL,
                PRIMARY KEY (poll_id, user_id),
                FOREIGN KEY (poll_id) REFERENCES stage_polls(id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_stage_polls_ends_at ON stage_polls(ends_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_stage_polls_pref_ends_at ON stage_polls(preference_ends_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_stage_polls_guild ON stage_polls(guild_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_stage_votes_poll ON stage_poll_votes(poll_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_stage_pref_votes_poll ON stage_poll_pref_votes(poll_id)")

        # Auto-translate preferences (per user per guild)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS auto_translate_prefs (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                source_lang TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                ttl_seconds INTEGER NOT NULL DEFAULT 10,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_auto_translate_guild_enabled ON auto_translate_prefs(guild_id, enabled)"
        )

        # Per-user first language preferences (default English if missing)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_language_prefs (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                first_language TEXT NOT NULL DEFAULT 'en',
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_language_guild ON user_language_prefs(guild_id)"
        )

        # Migration: drop old entries table (legacy card-based schema)
        await db.execute("DROP TABLE IF EXISTS entries")

        # Migration: add elo_settings to existing leaderboards
        try:
            await db.execute("ALTER TABLE leaderboards ADD COLUMN elo_settings TEXT DEFAULT '{}'")
        except aiosqlite.OperationalError:
            pass  # Column already exists

        # Migration: add stage poll preference timing columns
        try:
            await db.execute("ALTER TABLE stage_polls ADD COLUMN preference_duration_seconds INTEGER NOT NULL DEFAULT 0")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE stage_polls ADD COLUMN preference_ends_at REAL")
        except aiosqlite.OperationalError:
            pass

        try:
            await db.execute("ALTER TABLE member_entries ADD COLUMN match_count INTEGER NOT NULL DEFAULT 0")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE member_entries ADD COLUMN last_match_at REAL")
        except aiosqlite.OperationalError:
            pass

        try:
            await db.execute(
                "ALTER TABLE leaderboards ADD COLUMN match_format TEXT NOT NULL DEFAULT '1v1'"
            )
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute(
                "ALTER TABLE archetypes ADD COLUMN total_aa_matches INTEGER NOT NULL DEFAULT 0"
            )
        except aiosqlite.OperationalError:
            pass

        await db.commit()


def _default_settings_json() -> str:
    return json.dumps(EloSettings().to_dict())


async def create_leaderboard(
    guild_id: int,
    name: str,
    settings: EloSettings | None = None,
    match_format: str = "1v1",
) -> int | None:
    try:
        payload = json.dumps(settings.to_dict()) if settings else _default_settings_json()
        mf = match_format if match_format in ("1v1", "2v2") else "1v1"
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cur = await db.execute(
                """
                INSERT INTO leaderboards (guild_id, name, elo_settings, match_format)
                VALUES (?, ?, ?, ?) RETURNING id
                """,
                (guild_id, name.strip(), payload, mf),
            )
            row = await cur.fetchone()
            await db.commit()
            return row[0] if row else None
    except aiosqlite.IntegrityError:
        return None


async def list_leaderboards(guild_id: int) -> list[tuple[int, str, str]]:
    """Return (id, name, match_format) for each leaderboard."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, name, match_format FROM leaderboards WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )
        rows = await cur.fetchall()
        return [(r["id"], r["name"], r["match_format"] or "1v1") for r in rows]


async def get_leaderboard_id(guild_id: int, name: str) -> int | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "SELECT id FROM leaderboards WHERE guild_id = ? AND LOWER(name) = LOWER(?)",
            (guild_id, name.strip()),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def get_leaderboard_by_id(leaderboard_id: int) -> dict | None:
    """Return {id, guild_id, name, match_format} or None."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, guild_id, name, match_format FROM leaderboards WHERE id = ?",
            (leaderboard_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["match_format"] = d.get("match_format") or "1v1"
        return d


# Sentinel leaderboard_id for guild-wide tier list live messages
LIVE_TIERLIST_LEADERBOARD_ID = 0


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


async def get_leaderboard_elo_settings_raw(leaderboard_id: int) -> str | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "SELECT elo_settings FROM leaderboards WHERE id = ?",
            (leaderboard_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def set_leaderboard_elo_settings_raw(leaderboard_id: int, elo_settings_json: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "UPDATE leaderboards SET elo_settings = ? WHERE id = ?",
            (elo_settings_json, leaderboard_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def push_undo(guild_id: int, actor_id: int, payload: dict[str, Any]) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT INTO guild_undo_stack (guild_id, created_at, actor_id, payload)
            VALUES (?, ?, ?, ?)
            """,
            (guild_id, time.time(), actor_id, json.dumps(payload)),
        )
        await db.commit()


async def pop_and_apply_undo(guild_id: int) -> tuple[bool, str, dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, payload FROM guild_undo_stack
            WHERE guild_id = ? ORDER BY id DESC LIMIT 1
            """,
            (guild_id,),
        )
        row = await cur.fetchone()
        if not row:
            return False, "Nothing to undo for this server.", {}
        undo_id = int(row["id"])
        payload = json.loads(row["payload"])
        kind = payload["kind"]

        if kind == "match_1v1":
            p = payload
            for key in ("winner", "loser"):
                m = p[key]
                await db.execute(
                    """
                    UPDATE member_entries SET elo = ?, match_count = ?, last_match_at = ?
                    WHERE id = ?
                    """,
                    (m["elo"], m["match_count"], m["last_match_at"], m["row_id"]),
                )
            if not p.get("mirror"):
                wa = p["winner_arch"]
                la = p["loser_arch"]
                await db.execute(
                    "UPDATE archetypes SET elo = ?, total_aa_matches = ? WHERE guild_id = ? AND canonical_name = ?",
                    (wa["elo"], wa["total_aa_matches"], guild_id, wa["canonical"]),
                )
                await db.execute(
                    "UPDATE archetypes SET elo = ?, total_aa_matches = ? WHERE guild_id = ? AND canonical_name = ?",
                    (la["elo"], la["total_aa_matches"], guild_id, la["canonical"]),
                )
                pl, ph = p["pair_low"], p["pair_high"]
                n_before = int(p["pair_n_before"])
                if n_before <= 0:
                    await db.execute(
                        "DELETE FROM archetype_pair_counts WHERE guild_id = ? AND canon_a = ? AND canon_b = ?",
                        (guild_id, pl, ph),
                    )
                else:
                    await db.execute(
                        """
                        UPDATE archetype_pair_counts SET n = ?
                        WHERE guild_id = ? AND canon_a = ? AND canon_b = ?
                        """,
                        (n_before, guild_id, pl, ph),
                    )

        elif kind == "match_2v2":
            for m in payload["members"]:
                await db.execute(
                    """
                    UPDATE member_entries SET elo = ?, match_count = ?, last_match_at = ?
                    WHERE id = ?
                    """,
                    (m["elo"], m["match_count"], m["last_match_at"], m["row_id"]),
                )

        elif kind == "settings":
            await db.execute(
                "UPDATE leaderboards SET elo_settings = ? WHERE id = ?",
                (payload["prev_json"], int(payload["leaderboard_id"])),
            )

        elif kind == "aa_settings":
            prev = AaEloSettings.from_dict(payload["prev"])
            await db.execute(
                """
                UPDATE guild_aa_elo SET k_arch = ?, n0 = ?, influence_range = ?, min_games_display = ?
                WHERE guild_id = ?
                """,
                (
                    prev.k_arch,
                    prev.n0,
                    prev.influence_range,
                    prev.min_games_display,
                    guild_id,
                ),
            )

        else:
            return False, f"Unknown undo entry type: {kind}.", {}

        await db.execute("DELETE FROM guild_undo_stack WHERE id = ?", (undo_id,))
        await db.commit()

    labels = {
        "match_1v1": "1v1 match",
        "match_2v2": "2v2 match",
        "settings": "leaderboard settings",
        "aa_settings": "AA-ELO (archetype) settings",
    }
    hint: dict[str, Any] = {"kind": kind}
    if kind in ("match_1v1", "match_2v2"):
        hint["leaderboard_id"] = int(payload.get("leaderboard_id", 0))
        hint["refresh_tierlist"] = kind == "match_1v1"
    if kind == "settings":
        hint["leaderboard_id"] = int(payload.get("leaderboard_id", 0))
    if kind == "aa_settings":
        hint["refresh_tierlist"] = True
    return True, f"Undid the last action ({labels.get(kind, kind)}).", hint


async def get_aa_settings(guild_id: int) -> AaEloSettings:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO guild_aa_elo (guild_id) VALUES (?)",
            (guild_id,),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT k_arch, n0, influence_range, min_games_display FROM guild_aa_elo WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cur.fetchone()
        if not row:
            return AaEloSettings()
        return AaEloSettings(
            k_arch=float(row["k_arch"]),
            n0=float(row["n0"]),
            influence_range=float(row["influence_range"]),
            min_games_display=int(row["min_games_display"]),
        )


async def set_aa_settings(guild_id: int, settings: AaEloSettings) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO guild_aa_elo (guild_id) VALUES (?)",
            (guild_id,),
        )
        await db.execute(
            """
            UPDATE guild_aa_elo SET k_arch = ?, n0 = ?, influence_range = ?, min_games_display = ?
            WHERE guild_id = ?
            """,
            (
                settings.k_arch,
                settings.n0,
                settings.influence_range,
                settings.min_games_display,
                guild_id,
            ),
        )
        await db.commit()


async def get_pair_n(guild_id: int, canon_w: str, canon_l: str) -> int:
    low, high = _ordered_archetype_pair(canon_w, canon_l)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "SELECT n FROM archetype_pair_counts WHERE guild_id = ? AND canon_a = ? AND canon_b = ?",
            (guild_id, low, high),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def bump_archetype_pair_count(guild_id: int, canon_w: str, canon_l: str) -> None:
    low, high = _ordered_archetype_pair(canon_w, canon_l)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT INTO archetype_pair_counts (guild_id, canon_a, canon_b, n) VALUES (?, ?, ?, 1)
            ON CONFLICT(guild_id, canon_a, canon_b) DO UPDATE SET n = n + 1
            """,
            (guild_id, low, high),
        )
        await db.commit()


async def get_member_entry_full(leaderboard_id: int, user_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, elo, display_name, match_count, last_match_at
            FROM member_entries WHERE leaderboard_id = ? AND user_id = ?
            """,
            (leaderboard_id, user_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_archetype_stats(guild_id: int, canonical: str) -> tuple[float, int] | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT elo, total_aa_matches FROM archetypes WHERE guild_id = ? AND canonical_name = ?",
            (guild_id, canonical),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return (float(row["elo"]), int(row["total_aa_matches"] or 0))


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
    r = await get_member_entry_full(leaderboard_id, user_id)
    if not r:
        return None
    return (int(r["id"]), float(r["elo"]), r["display_name"])


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
            """
            INSERT INTO archetypes (guild_id, canonical_name, display_name, elo, total_aa_matches)
            VALUES (?, ?, ?, 1000, 0)
            """,
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
    *,
    actor_id: int | None = None,
) -> bool:
    """Record 1v1 match: member ELO + AA-ELO archetype track (guild-wide)."""
    meta = await get_leaderboard_by_id(leaderboard_id)
    if not meta or int(meta["guild_id"]) != guild_id:
        return False
    if meta.get("match_format", "1v1") != "1v1":
        return False

    wrow = await get_member_entry_full(leaderboard_id, winner_user_id)
    lrow = await get_member_entry_full(leaderboard_id, loser_user_id)
    if not wrow or not lrow:
        return False

    settings = await get_leaderboard_settings(leaderboard_id)
    if settings.locked:
        return False

    wid, lid = int(wrow["id"]), int(lrow["id"])
    welo_pre, lelo_pre = float(wrow["elo"]), float(lrow["elo"])
    wdelta, ldelta = elo_change(welo_pre, lelo_pre, settings)
    new_welo = welo_pre + wdelta
    new_lelo = lelo_pre + ldelta

    winner_canon = normalize_deck_name(winner_deck)
    loser_canon = normalize_deck_name(loser_deck)
    mirror = winner_canon == loser_canon

    await get_or_create_archetype(guild_id, winner_deck)
    await get_or_create_archetype(guild_id, loser_deck)

    w_stats = await get_archetype_stats(guild_id, winner_canon)
    l_stats = await get_archetype_stats(guild_id, loser_canon)
    if not w_stats or not l_stats:
        return False
    w_arch_pre, w_ta_pre = w_stats
    l_arch_pre, l_ta_pre = l_stats

    aa = await get_aa_settings(guild_id)
    inf = aa.influence_range
    pair_n_before = 0 if mirror else await get_pair_n(guild_id, winner_canon, loser_canon)

    delta_arch = 0.0
    if not mirror:
        e_player = aa_elo_expected(new_welo, new_lelo, inf)
        e_arch = aa_elo_expected(w_arch_pre, l_arch_pre, inf)
        alpha = 2.0 * abs(e_player - 0.5)
        e_combined = alpha * e_player + (1.0 - alpha) * e_arch
        denom = pair_n_before + aa.n0
        k_eff = aa.k_arch * (pair_n_before / denom) if denom > 0 else 0.0
        delta_arch = k_eff * (1.0 - e_combined)

    now = time.time()
    w_mc, w_lm = int(wrow["match_count"] or 0), wrow["last_match_at"]
    l_mc, l_lm = int(lrow["match_count"] or 0), lrow["last_match_at"]

    undo: dict[str, Any] = {
        "kind": "match_1v1",
        "leaderboard_id": leaderboard_id,
        "mirror": mirror,
        "winner": {
            "row_id": wid,
            "elo": welo_pre,
            "match_count": w_mc,
            "last_match_at": w_lm,
        },
        "loser": {
            "row_id": lid,
            "elo": lelo_pre,
            "match_count": l_mc,
            "last_match_at": l_lm,
        },
    }
    if not mirror:
        pl, ph = _ordered_archetype_pair(winner_canon, loser_canon)
        undo["pair_low"] = pl
        undo["pair_high"] = ph
        undo["pair_n_before"] = pair_n_before
        undo["winner_arch"] = {
            "canonical": winner_canon,
            "elo": w_arch_pre,
            "total_aa_matches": w_ta_pre,
        }
        undo["loser_arch"] = {
            "canonical": loser_canon,
            "elo": l_arch_pre,
            "total_aa_matches": l_ta_pre,
        }

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE member_entries SET elo = elo + ?, match_count = match_count + 1, last_match_at = ? WHERE id = ?",
            (wdelta, now, wid),
        )
        await db.execute(
            "UPDATE member_entries SET elo = elo + ?, match_count = match_count + 1, last_match_at = ? WHERE id = ?",
            (ldelta, now, lid),
        )
        if not mirror:
            await db.execute(
                "UPDATE archetypes SET elo = elo + ?, total_aa_matches = total_aa_matches + 1 WHERE guild_id = ? AND canonical_name = ?",
                (delta_arch, guild_id, winner_canon),
            )
            await db.execute(
                "UPDATE archetypes SET elo = elo - ?, total_aa_matches = total_aa_matches + 1 WHERE guild_id = ? AND canonical_name = ?",
                (delta_arch, guild_id, loser_canon),
            )
            await bump_archetype_pair_count(guild_id, winner_canon, loser_canon)
        await db.commit()

    if actor_id is not None:
        await push_undo(guild_id, actor_id, undo)
    return True


async def record_match_2v2(
    guild_id: int,
    leaderboard_id: int,
    winner1_id: int,
    winner2_id: int,
    loser1_id: int,
    loser2_id: int,
    winner1_deck: str,
    winner2_deck: str,
    loser1_deck: str,
    loser2_deck: str,
    *,
    actor_id: int | None = None,
) -> bool:
    """2v2: team average ELO vs team average; no archetype AA-ELO updates."""
    meta = await get_leaderboard_by_id(leaderboard_id)
    if not meta or int(meta["guild_id"]) != guild_id:
        return False
    if meta.get("match_format", "1v1") != "2v2":
        return False

    ids = {winner1_id, winner2_id, loser1_id, loser2_id}
    if len(ids) < 4:
        return False

    settings = await get_leaderboard_settings(leaderboard_id)
    if settings.locked:
        return False

    uids = [winner1_id, winner2_id, loser1_id, loser2_id]
    row_list: list[dict[str, Any]] = []
    for uid in uids:
        r = await get_member_entry_full(leaderboard_id, uid)
        if not r:
            return False
        row_list.append(r)

    w_elos = [float(row_list[0]["elo"]), float(row_list[1]["elo"])]
    l_elos = [float(row_list[2]["elo"]), float(row_list[3]["elo"])]
    team_w = (w_elos[0] + w_elos[1]) / 2.0
    team_l = (l_elos[0] + l_elos[1]) / 2.0
    wdelta, ldelta = elo_change(team_w, team_l, settings)
    now = time.time()

    undo_members = []
    for r in row_list:
        undo_members.append(
            {
                "row_id": int(r["id"]),
                "elo": float(r["elo"]),
                "match_count": int(r["match_count"] or 0),
                "last_match_at": r["last_match_at"],
            }
        )

    win_set = {winner1_id, winner2_id}
    async with aiosqlite.connect(DATABASE_PATH) as db:
        for uid, r in zip(uids, row_list):
            rid = int(r["id"])
            side_delta = wdelta if uid in win_set else ldelta
            await db.execute(
                """
                UPDATE member_entries SET elo = elo + ?, match_count = match_count + 1, last_match_at = ?
                WHERE id = ?
                """,
                (side_delta, now, rid),
            )
        await db.commit()

    if actor_id is not None:
        await push_undo(
            guild_id,
            actor_id,
            {"kind": "match_2v2", "leaderboard_id": leaderboard_id, "members": undo_members},
        )
    return True


async def get_member_leaderboard(leaderboard_id: int, limit: int = 25) -> list[tuple[int, str | None, float]]:
    """Return list of (user_id, display_name, elo) sorted by ELO descending (after filters)."""
    settings = await get_leaderboard_settings(leaderboard_id)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT user_id, display_name, elo, match_count, last_match_at
            FROM member_entries WHERE leaderboard_id = ?
            """,
            (leaderboard_id,),
        )
        rows = await cur.fetchall()
    now = time.time()
    min_m = settings.matches_required_for_ranking
    inactive_d = settings.inactive_days_threshold
    out: list[tuple[int, str | None, float]] = []
    for r in rows:
        mc = int(r["match_count"] or 0)
        lm = r["last_match_at"]
        if min_m > 0 and mc < min_m:
            continue
        if inactive_d > 0 and lm is not None and (now - float(lm)) > inactive_d * 86400:
            continue
        out.append((r["user_id"], r["display_name"], r["elo"]))
    out.sort(key=lambda t: -t[2])
    return out[:limit]


async def get_tier_list(guild_id: int, limit: int = 25) -> list[tuple[str, str, float]]:
    """Return archetypes with enough AA-ELO sample games, sorted by ELO descending."""
    aa = await get_aa_settings(guild_id)
    min_g = max(0, int(aa.min_games_display))
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            """
            SELECT canonical_name, display_name, elo FROM archetypes
            WHERE guild_id = ? AND total_aa_matches >= ?
            ORDER BY elo DESC LIMIT ?
            """,
            (guild_id, min_g, limit),
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
            "UPDATE member_entries SET elo = ?, match_count = 0, last_match_at = NULL WHERE leaderboard_id = ?",
            (settings.default_rating, lb_id),
        )
        await db.commit()
    return True


async def reset_tier_list(guild_id: int) -> bool:
    """Reset archetype AA-ELO, matchup counts, and per-archetype game totals."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM archetype_pair_counts WHERE guild_id = ?", (guild_id,))
        await db.execute(
            "UPDATE archetypes SET elo = 1000, total_aa_matches = 0 WHERE guild_id = ?",
            (guild_id,),
        )
        await db.commit()
    return True


async def delete_leaderboard(guild_id: int, name: str) -> bool:
    lb_id = await get_leaderboard_id(guild_id, name)
    if not lb_id:
        return False
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM leaderboards WHERE id = ?", (lb_id,))
        await db.commit()
    return True


# --- Live leaderboard / tier list channel messages ---


async def upsert_live_display(
    guild_id: int,
    leaderboard_id: int,
    kind: str,
    channel_id: int,
    message_id: int,
) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO live_leaderboard_messages (guild_id, leaderboard_id, kind, channel_id, message_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, leaderboard_id, channel_id, kind)
            DO UPDATE SET message_id = excluded.message_id
            """,
            (guild_id, leaderboard_id, kind, channel_id, message_id),
        )
        await conn.commit()


async def get_live_displays(guild_id: int, leaderboard_id: int, kind: str) -> list[tuple[int, int, int]]:
    """Return list of (row_id, channel_id, message_id)."""
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute(
            """
            SELECT id, channel_id, message_id FROM live_leaderboard_messages
            WHERE guild_id = ? AND leaderboard_id = ? AND kind = ?
            """,
            (guild_id, leaderboard_id, kind),
        )
        return [(r[0], r[1], r[2]) for r in await cur.fetchall()]


async def delete_live_display_row(row_id: int) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute("DELETE FROM live_leaderboard_messages WHERE id = ?", (row_id,))
        await conn.commit()


async def delete_live_rankings_for_leaderboard(guild_id: int, leaderboard_id: int) -> list[tuple[int, int]]:
    """Remove DB rows; return (channel_id, message_id) for optional Discord cleanup."""
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute(
            """
            SELECT channel_id, message_id FROM live_leaderboard_messages
            WHERE guild_id = ? AND leaderboard_id = ? AND kind = 'rankings'
            """,
            (guild_id, leaderboard_id),
        )
        pairs = [(r[0], r[1]) for r in await cur.fetchall()]
        await conn.execute(
            """
            DELETE FROM live_leaderboard_messages
            WHERE guild_id = ? AND leaderboard_id = ? AND kind = 'rankings'
            """,
            (guild_id, leaderboard_id),
        )
        await conn.commit()
    return pairs


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


# --- Two-stage tier polls ---

async def create_stage_poll(
    guild_id: int,
    channel_id: int,
    title: str,
    options: list[str],
    role_ids: list[int],
    num_tiers: int,
    duration_seconds: int,
    preference_duration_seconds: int,
) -> int:
    now = time.time()
    ends_at = now + duration_seconds
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute(
            """
            INSERT INTO stage_polls (
                guild_id, channel_id, title, options, role_ids, num_tiers, ends_at, created_at, preference_duration_seconds
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                title,
                json.dumps(options),
                json.dumps(role_ids),
                num_tiers,
                ends_at,
                now,
                preference_duration_seconds,
            ),
        )
        await conn.commit()
        return cur.lastrowid


async def get_stage_poll_by_id(poll_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM stage_polls WHERE id = ?", (poll_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def add_stage_vote(poll_id: int, user_id: int, option_index: int, tier: int) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO stage_poll_votes (poll_id, user_id, option_index, tier)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(poll_id, user_id, option_index)
            DO UPDATE SET tier = excluded.tier
            """,
            (poll_id, user_id, option_index, tier),
        )
        await conn.commit()


async def get_stage_votes(poll_id: int) -> list[tuple[int, int, int]]:
    """Return (user_id, option_index, tier)."""
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute(
            "SELECT user_id, option_index, tier FROM stage_poll_votes WHERE poll_id = ?",
            (poll_id,),
        )
        return [(r[0], r[1], r[2]) for r in await cur.fetchall()]


async def set_stage_poll_status(
    poll_id: int,
    status: str,
    *,
    preference_options: list[int] | None = None,
    preference_ends_at: float | None = None,
    attempts: int | None = None,
) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        pref_json = None if preference_options is None else json.dumps(preference_options)
        if attempts is None:
            await conn.execute(
                "UPDATE stage_polls SET status = ?, preference_options = ?, preference_ends_at = ? WHERE id = ?",
                (status, pref_json, preference_ends_at, poll_id),
            )
        else:
            await conn.execute(
                "UPDATE stage_polls SET status = ?, preference_options = ?, preference_ends_at = ?, attempts = ? WHERE id = ?",
                (status, pref_json, preference_ends_at, attempts, poll_id),
            )
        await conn.commit()


async def add_stage_preference_vote(poll_id: int, user_id: int, option_index: int) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO stage_poll_pref_votes (poll_id, user_id, option_index)
            VALUES (?, ?, ?)
            ON CONFLICT(poll_id, user_id)
            DO UPDATE SET option_index = excluded.option_index
            """,
            (poll_id, user_id, option_index),
        )
        await conn.commit()


async def get_stage_preference_votes(poll_id: int) -> list[tuple[int, int]]:
    """Return (user_id, option_index)."""
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute(
            "SELECT user_id, option_index FROM stage_poll_pref_votes WHERE poll_id = ?",
            (poll_id,),
        )
        return [(r[0], r[1]) for r in await cur.fetchall()]


async def clear_stage_preference_votes(poll_id: int) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute("DELETE FROM stage_poll_pref_votes WHERE poll_id = ?", (poll_id,))
        await conn.commit()


async def get_pending_stage1_polls() -> list[dict]:
    now = time.time()
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM stage_polls WHERE status = 'stage1_open' AND ends_at <= ?",
            (now,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_pending_preference_polls() -> list[dict]:
    now = time.time()
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """
            SELECT * FROM stage_polls
            WHERE status = 'preference_open' AND preference_ends_at IS NOT NULL AND preference_ends_at <= ?
            """,
            (now,),
        )
        return [dict(r) for r in await cur.fetchall()]


# --- Auto-translate preferences ---

async def upsert_auto_translate_pref(
    guild_id: int,
    user_id: int,
    source_lang: str,
    target_lang: str,
    ttl_seconds: int,
    enabled: bool = True,
) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO auto_translate_prefs (guild_id, user_id, source_lang, target_lang, enabled, ttl_seconds)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET
                source_lang = excluded.source_lang,
                target_lang = excluded.target_lang,
                enabled = excluded.enabled,
                ttl_seconds = excluded.ttl_seconds
            """,
            (guild_id, user_id, source_lang.lower().strip(), target_lang.lower().strip(), 1 if enabled else 0, ttl_seconds),
        )
        await conn.commit()


async def disable_auto_translate_pref(guild_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute(
            "UPDATE auto_translate_prefs SET enabled = 0 WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def get_auto_translate_pref(guild_id: int, user_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM auto_translate_prefs WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_enabled_auto_translate_prefs(guild_id: int) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM auto_translate_prefs WHERE guild_id = ? AND enabled = 1",
            (guild_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def set_user_first_language(guild_id: int, user_id: int, lang_code: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO user_language_prefs (guild_id, user_id, first_language)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET first_language = excluded.first_language
            """,
            (guild_id, user_id, lang_code.lower().strip()),
        )
        await conn.commit()


async def get_user_first_language(guild_id: int, user_id: int) -> str:
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute(
            "SELECT first_language FROM user_language_prefs WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        return (row[0] if row and row[0] else "en").lower().strip()


async def get_guild_first_languages(guild_id: int) -> dict[int, str]:
    """Return {user_id: first_language}. Missing users imply English."""
    async with aiosqlite.connect(DATABASE_PATH) as conn:
        cur = await conn.execute(
            "SELECT user_id, first_language FROM user_language_prefs WHERE guild_id = ?",
            (guild_id,),
        )
        return {int(r[0]): str(r[1]).lower().strip() for r in await cur.fetchall()}
