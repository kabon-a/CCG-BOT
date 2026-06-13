"""Card releases cog — post published card images to Discord.

When an admin clicks "Publish" on the Interspace, the backend marks the
submission's approved entry as published and renders a finished YGO card image
for each card. This cog polls Interspace for those pending releases, downloads
each generated image, and posts it to the ``❗❗-card-releases-ccg`` channel with:

    @Customs Updates
    By @<creator>

one message per card. After all of a submission's cards are posted it tells
Interspace to mark the submission posted so it is not posted again.

Mirrors the Interspace-backed ``@tasks.loop`` pattern in ``cogs/link.py`` and the
name-based channel resolution in ``cogs/poll.py``.
"""

import asyncio
import io

import aiohttp
import discord
from discord.ext import commands, tasks

from config import INTERSPACE_URL, INTERSPACE_BOT_SECRET

CARD_RELEASES_CHANNEL_NAME = "❗❗-card-releases-ccg"
CUSTOMS_UPDATES_ROLE_NAME = "Customs Updates"
POLL_INTERVAL_SECONDS = 30


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


def setup(bot: commands.Bot) -> None:
    bot.add_cog(CardReleasesCog(bot))


class CardReleasesCog(commands.Cog):
    """Polls Interspace for published cards and posts their images."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.poll_card_releases.is_running():
            self.poll_card_releases.start()

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def poll_card_releases(self) -> None:
        status, body = await _interspace_get("/api/discord/card-releases/pending")
        if status != 200 or not isinstance(body, dict):
            return
        releases = body.get("releases") or []
        if not releases:
            return

        for release in releases:
            # Only post once every card image is rendered and ready.
            if not release.get("ready"):
                continue
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

    async def _post_release(self, release: dict) -> None:
        submission_id = release.get("submissionId")
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

        posted_all = True
        for card in cards:
            image_url = card.get("imageUrl")
            file_name = card.get("fileName") or f"{card.get('name', 'card')}.png"
            if not image_url:
                posted_all = False
                continue
            image_bytes = await _interspace_get_bytes(image_url)
            if not image_bytes:
                posted_all = False
                continue
            try:
                discord_file = discord.File(io.BytesIO(image_bytes), filename=file_name)
                await target_channel.send(
                    content=message,
                    file=discord_file,
                    allowed_mentions=allowed,
                )
            except Exception as exc:
                print(f"[card-releases] failed to post {file_name}: {exc}")
                posted_all = False
            await asyncio.sleep(1)

        if posted_all:
            await _interspace_post(
                f"/api/discord/card-releases/{submission_id}/mark-posted",
                {},
            )

    @poll_card_releases.before_loop
    async def before_poll_card_releases(self) -> None:
        await self.bot.wait_until_ready()
