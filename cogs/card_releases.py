"""Card releases cog — post published card images to Discord.

When an admin clicks "Publish" on the Interspace, the backend marks the
submission's approved entry as published and renders a finished YGO card image
for each card. This cog polls Interspace for those pending releases, downloads
each generated image, and posts it to the ``❗❗-card-releases-ccg`` channel with:

    @Customs Updates
    By @<creator>

one message per card. A card is claimed in the local database before it is
sent, and an existing bot message for that same card is kept instead of
posting another copy. After every card in the submission is posted, the cog
tells Interspace to mark it posted and only treats a 2xx response as success.

Mirrors the Interspace-backed ``@tasks.loop`` pattern in ``cogs/link.py`` and the
name-based channel resolution in ``cogs/poll.py``.
"""

import asyncio
import hashlib
import io
import time
import uuid
from datetime import timedelta

import aiohttp
import aiosqlite
import discord
from discord.ext import commands, tasks

from config import DATABASE_PATH, INTERSPACE_URL, INTERSPACE_BOT_SECRET

CARD_RELEASES_CHANNEL_NAME = "❗❗-card-releases-ccg"
CUSTOMS_UPDATES_ROLE_NAME = "Customs Updates"
POLL_INTERVAL_SECONDS = 30
# Long enough for a slow image upload, short enough that a crash can retry.
_CLAIM_TTL_SECONDS = 180
# Recent bot posts of this same file are the in-flight duplicate, not a new publish.
_DUPLICATE_LOOKBACK = timedelta(minutes=3)


def _interspace_headers() -> dict:
    return {"x-bot-secret": INTERSPACE_BOT_SECRET, "Content-Type": "application/json"}


async def _interspace_get(path: str) -> tuple[int, dict | None]:
    """GET JSON from Interspace. Returns (status_code, json_body_or_none)."""
    if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
        return 0, None
    url = f"{INTERSPACE_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=_interspace_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                ct = resp.headers.get("content-type", "")
                if "application/json" in ct:
                    return resp.status, await resp.json()
                return resp.status, {"text": (await resp.text())[:200]}
    except Exception as exc:
        print(f"[card-releases] GET {path} failed: {exc}")
        return 0, None


async def _interspace_get_bytes(path: str) -> bytes | None:
    """GET raw bytes (a generated card image) from Interspace."""
    if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
        return None
    url = f"{INTERSPACE_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"x-bot-secret": INTERSPACE_BOT_SECRET},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    print(f"[card-releases] image GET {path} -> {resp.status}")
                    return None
                return await resp.read()
    except Exception as exc:
        print(f"[card-releases] image GET {path} failed: {exc}")
        return None


async def _interspace_post(path: str, payload: dict) -> tuple[int, dict | None]:
    if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
        return 0, None
    url = f"{INTERSPACE_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=_interspace_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                ct = resp.headers.get("content-type", "")
                if "application/json" in ct:
                    return resp.status, await resp.json()
                return resp.status, {"text": (await resp.text())[:200]}
    except Exception as exc:
        print(f"[card-releases] POST {path} failed: {exc}")
        return 0, None


def _resolve_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Find the card-releases channel by name, tolerating ``_`` vs ``-``."""
    channel = discord.utils.get(guild.text_channels, name=CARD_RELEASES_CHANNEL_NAME)
    if channel is not None:
        return channel
    target = CARD_RELEASES_CHANNEL_NAME.replace("_", "-")
    return next(
        (c for c in guild.text_channels if c.name.replace("_", "-") == target),
        None,
    )


def _card_key(card: dict) -> str:
    """Stable identity for one card inside a submission."""
    for field in ("id", "cardId", "entryId"):
        value = card.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    image = str(card.get("imageUrl") or "").strip()
    if image:
        return image
    return str(card.get("fileName") or card.get("name") or "").strip()


def _dedupe_releases(releases: list) -> list[dict]:
    """One ready release per submission, with repeated cards removed."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for release in releases:
        if not isinstance(release, dict) or not release.get("ready"):
            continue
        submission_id = str(release.get("submissionId") or "").strip()
        if not submission_id:
            continue
        cards = [card for card in (release.get("cards") or []) if isinstance(card, dict)]
        if submission_id not in merged:
            merged[submission_id] = {**release, "submissionId": submission_id, "cards": []}
            order.append(submission_id)
        merged[submission_id]["cards"].extend(cards)

    ready: list[dict] = []
    for submission_id in order:
        release = merged[submission_id]
        seen: set[str] = set()
        unique: list[dict] = []
        for index, card in enumerate(release["cards"]):
            key = _card_key(card) or f"{submission_id}:{index}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(card)
        if unique:
            release["cards"] = unique
            ready.append(release)
    return ready


def _release_marker(submission_id: str, card_key: str) -> str:
    """Attachment description that identifies this exact posted card."""
    marker = f"ccg-release:{submission_id}:{card_key}".replace("\r", " ").replace("\n", " ")
    if len(marker) <= 1024:
        return marker
    return "ccg-release:" + hashlib.sha256(marker.encode()).hexdigest()


def _card_file(image_bytes: bytes, file_name: str, marker: str) -> discord.File:
    """Build the upload, including the marker when this discord build supports it."""
    buffer = io.BytesIO(image_bytes)
    try:
        return discord.File(buffer, filename=file_name, description=marker)
    except TypeError:
        buffer.seek(0)
        return discord.File(buffer, filename=file_name)


def _message_is_card(
    message: discord.Message,
    *,
    bot_id: int,
    marker: str,
    file_name: str,
    content: str,
) -> bool:
    """True when this bot message is a post of the card we are about to send."""
    if message.author.id != bot_id:
        return False
    for attachment in message.attachments:
        description = getattr(attachment, "description", None) or ""
        if description == marker:
            return True
        # Posts from before markers existed: same file and same announcement.
        if description.startswith("ccg-release:"):
            continue
        if attachment.filename == file_name and (message.content or "") == content:
            return True
    return False


def setup(bot: commands.Bot) -> None:
    bot.add_cog(CardReleasesCog(bot))


async def _ensure_ledger() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH, timeout=5) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS card_release_posts (
                submission_id TEXT NOT NULL,
                card_key TEXT NOT NULL,
                status TEXT NOT NULL,
                claimed_at REAL NOT NULL,
                claim_token TEXT,
                PRIMARY KEY (submission_id, card_key)
            )
            """
        )
        await db.commit()


async def _claim_card(submission_id: str, card_key: str) -> tuple[str, str]:
    """Reserve the right to send this card.

    Returns ``(send, token)`` when this caller should post it, ``(posted, "")``
    when a copy was already sent, or ``(busy, "")`` when another caller holds
    a fresh claim. The insert-or-steal is one statement so two pollers cannot
    both win.
    """
    token = uuid.uuid4().hex
    now = time.time()
    cutoff = now - _CLAIM_TTL_SECONDS
    async with aiosqlite.connect(DATABASE_PATH, timeout=5) as db:
        await db.execute(
            """
            INSERT INTO card_release_posts (
                submission_id, card_key, status, claimed_at, claim_token
            )
            VALUES (?, ?, 'claimed', ?, ?)
            ON CONFLICT(submission_id, card_key) DO UPDATE SET
                status = 'claimed',
                claimed_at = excluded.claimed_at,
                claim_token = excluded.claim_token
            WHERE card_release_posts.status = 'claimed'
              AND card_release_posts.claimed_at <= ?
            """,
            (submission_id, card_key, now, token, cutoff),
        )
        cursor = await db.execute(
            """
            SELECT status, claim_token FROM card_release_posts
            WHERE submission_id = ? AND card_key = ?
            """,
            (submission_id, card_key),
        )
        row = await cursor.fetchone()
        await db.commit()
    if row is None:
        return "busy", ""
    status, owner = row
    if status == "posted":
        return "posted", ""
    if owner == token:
        return "send", token
    return "busy", ""


async def _mark_card_posted(submission_id: str, card_key: str) -> None:
    now = time.time()
    async with aiosqlite.connect(DATABASE_PATH, timeout=5) as db:
        await db.execute(
            """
            INSERT INTO card_release_posts (
                submission_id, card_key, status, claimed_at, claim_token
            )
            VALUES (?, ?, 'posted', ?, NULL)
            ON CONFLICT(submission_id, card_key) DO UPDATE SET
                status = 'posted',
                claimed_at = excluded.claimed_at,
                claim_token = NULL
            """,
            (submission_id, card_key, now),
        )
        await db.commit()


async def _release_claim(submission_id: str, card_key: str, token: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH, timeout=5) as db:
        await db.execute(
            """
            DELETE FROM card_release_posts
            WHERE submission_id = ? AND card_key = ? AND status = 'claimed' AND claim_token = ?
            """,
            (submission_id, card_key, token),
        )
        await db.commit()


class CardReleasesCog(commands.Cog):
    """Polls Interspace for published cards and posts their images."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._poll_lock = asyncio.Lock()

    def cog_unload(self) -> None:
        self.poll_card_releases.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.poll_card_releases.is_running():
            self.poll_card_releases.start()

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def poll_card_releases(self) -> None:
        async with self._poll_lock:
            await self._poll_once()

    async def _poll_once(self) -> None:
        await _ensure_ledger()
        status, body = await _interspace_get("/api/discord/card-releases/pending")
        if status != 200 or not isinstance(body, dict):
            return
        releases = _dedupe_releases(body.get("releases") or [])
        if not releases:
            return

        for release in releases:
            await self._post_release(release)
            # Stay polite with the Discord API between submissions.
            await asyncio.sleep(1)

    def _build_message(self, guild: discord.Guild, release: dict) -> str:
        role = discord.utils.get(guild.roles, name=CUSTOMS_UPDATES_ROLE_NAME)
        role_mention = role.mention if role else f"@{CUSTOMS_UPDATES_ROLE_NAME}"

        discord_id = release.get("creatorDiscordId")
        if discord_id:
            creator_mention = f"<@{discord_id}>"
        else:
            name = release.get("creatorDiscordUsername") or release.get("creatorName") or "Unknown"
            creator_mention = f"@{name}"

        return f"{role_mention}\nBy {creator_mention}"

    async def _find_existing_posts(
        self,
        channel: discord.TextChannel,
        *,
        marker: str,
        file_name: str,
        content: str,
    ) -> list[discord.Message] | None:
        """Bot posts of this card in the recent channel history.

        ``None`` means history could not be read, so the caller must not treat
        the card as already posted.
        """
        if self.bot.user is None:
            return None
        after = discord.utils.utcnow() - _DUPLICATE_LOOKBACK
        found: list[discord.Message] = []
        try:
            async for message in channel.history(limit=100, after=after):
                if _message_is_card(
                    message,
                    bot_id=self.bot.user.id,
                    marker=marker,
                    file_name=file_name,
                    content=content,
                ):
                    found.append(message)
        except discord.HTTPException as exc:
            print(f"[card-releases] could not read #{channel.name} history: {exc}")
            return None
        return found

    async def _delete_extra_posts(self, messages: list[discord.Message]) -> None:
        """Keep the oldest post of a card and delete the copies."""
        if len(messages) < 2:
            return
        keep = min(messages, key=lambda message: message.id)
        removed = 0
        for message in messages:
            if message.id == keep.id:
                continue
            try:
                await message.delete()
                removed += 1
            except discord.NotFound:
                removed += 1
            except discord.HTTPException as exc:
                print(f"[card-releases] failed to delete duplicate message {message.id}: {exc}")
        if removed:
            print(f"[card-releases] removed {removed} duplicate post(s), kept message {keep.id}")

    async def _post_release(self, release: dict) -> None:
        submission_id = str(release.get("submissionId") or "").strip()
        cards = release.get("cards") or []
        if not submission_id or not cards:
            return

        # Find the first guild that actually has the releases channel.
        target_channel = None
        for guild in self.bot.guilds:
            channel = _resolve_channel(guild)
            if channel is not None:
                target_channel = channel
                break
        if target_channel is None:
            print(f"[card-releases] channel '{CARD_RELEASES_CHANNEL_NAME}' not found in any guild")
            return

        message = self._build_message(target_channel.guild, release)
        allowed = discord.AllowedMentions(roles=True, users=True, everyone=False)
        incomplete = False

        for index, card in enumerate(cards):
            if not isinstance(card, dict):
                incomplete = True
                continue
            card_key = _card_key(card) or f"{submission_id}:{index}"
            image_url = card.get("imageUrl")
            file_name = card.get("fileName") or f"{card.get('name', 'card')}.png"
            if not image_url:
                incomplete = True
                continue

            state, claim_token = await _claim_card(submission_id, card_key)
            marker = _release_marker(submission_id, card_key)
            if state == "busy":
                incomplete = True
                continue
            if state == "posted":
                existing = await self._find_existing_posts(
                    target_channel,
                    marker=marker,
                    file_name=file_name,
                    content=message,
                )
                if existing:
                    await self._delete_extra_posts(existing)
                continue

            existing = await self._find_existing_posts(
                target_channel,
                marker=marker,
                file_name=file_name,
                content=message,
            )
            # ``None`` means history could not be read. Send anyway; the claim
            # stops this process from posting the card again on the next poll.
            if existing:
                await self._delete_extra_posts(existing)
                await _mark_card_posted(submission_id, card_key)
                continue

            image_bytes = await _interspace_get_bytes(image_url)
            if not image_bytes:
                await _release_claim(submission_id, card_key, claim_token)
                incomplete = True
                continue
            try:
                discord_file = _card_file(image_bytes, file_name, marker)
                await target_channel.send(
                    content=message,
                    file=discord_file,
                    allowed_mentions=allowed,
                )
            except Exception as exc:
                print(f"[card-releases] failed to post {file_name}: {exc}")
                await _release_claim(submission_id, card_key, claim_token)
                incomplete = True
                await asyncio.sleep(1)
                continue

            await _mark_card_posted(submission_id, card_key)
            posted = await self._find_existing_posts(
                target_channel,
                marker=marker,
                file_name=file_name,
                content=message,
            )
            if posted:
                await self._delete_extra_posts(posted)
            await asyncio.sleep(1)

        if incomplete:
            return

        status, body = await _interspace_post(
            f"/api/discord/card-releases/{submission_id}/mark-posted",
            {},
        )
        if status < 200 or status >= 300:
            print(f"[card-releases] mark-posted {submission_id} -> {status} {body}")

    @poll_card_releases.before_loop
    async def before_poll_card_releases(self) -> None:
        await self.bot.wait_until_ready()
