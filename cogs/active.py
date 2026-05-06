"""Active cog — assign @active role to users with recent activity (within 7 days).

Activity is recorded on three flavours of Discord events:
  * messages
  * reactions (legacy ``on_reaction_add`` + raw payload for cache-miss cases)
  * any slash-command interaction

Every grant also pings Interspace via ``/api/discord/active-ping`` so that
linked Interspace users count toward the 65% poll-quorum on the web side.

A periodic task pulls ``/api/discord/active-pulse`` from Interspace, which
returns every linked Discord user who has been active on the *Interspace*
in the past 7 days. We then grant @active to those Discord users, mirroring
their web activity onto Discord — without that, somebody who only ever uses
Interspace would lose @active and be excluded from poll quorums.
"""

from __future__ import annotations

import aiohttp
import discord
from discord.ext import commands, tasks

import database as db
from config import INTERSPACE_URL, INTERSPACE_BOT_SECRET

ACTIVE_ROLE_NAME = "active"
INTERSPACE_PULSE_INTERVAL_MINUTES = 5


def setup(bot: commands.Bot) -> None:
    bot.add_cog(ActiveCog(bot))


def _interspace_headers() -> dict:
    return {"x-bot-secret": INTERSPACE_BOT_SECRET, "Content-Type": "application/json"}


async def _interspace_post(path: str, payload: dict) -> dict | None:
    if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
        return None
    url = f"{INTERSPACE_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=_interspace_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                return None
    except Exception as exc:  # pragma: no cover — best-effort fire-and-forget
        print(f"[Interspace] POST {path} failed: {exc}")
        return None


async def _interspace_get(path: str) -> dict | None:
    if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
        return None
    url = f"{INTERSPACE_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=_interspace_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
    except Exception as exc:  # pragma: no cover
        print(f"[Interspace] GET {path} failed: {exc}")
        return None


async def ensure_active_role(guild: discord.Guild) -> discord.Role | None:
    """Get or create the @active role. Returns None if bot lacks permissions."""
    role = discord.utils.get(guild.roles, name=ACTIVE_ROLE_NAME)
    if role:
        return role
    try:
        role = await guild.create_role(
            name=ACTIVE_ROLE_NAME,
            reason="Active member tracking (courtroom policies)",
        )
        return role
    except discord.Forbidden:
        return None


async def grant_active_and_record(
    guild: discord.Guild,
    user: discord.Member | discord.User,
) -> None:
    """Grant @active role and record activity. Idempotent.

    Also pings Interspace so the linked user (if any) is counted as active
    on the web side. The Interspace ping is fire-and-forget — we never let
    a network blip block the local @active assignment.
    """
    if not guild or not user:
        return
    if user.bot:
        return
    member = guild.get_member(user.id) if isinstance(user, discord.User) else user
    if not member:
        return
    await db.record_activity(guild.id, user.id)
    role = await ensure_active_role(guild)
    if role and role not in member.roles:
        try:
            await member.add_roles(role, reason="Activity recorded")
        except discord.Forbidden:
            pass

    # Cross-platform mirror: tell Interspace this Discord user just was active.
    # Only meaningful if they've linked their Interspace account, but the
    # endpoint silently no-ops for unlinked users so we don't gate on that.
    try:
        await _interspace_post("/api/discord/active-ping", {"discordId": str(user.id)})
    except Exception:
        pass


async def _remove_stale_active_impl(bot: commands.Bot) -> None:
    """Remove @active from users with no activity in 7+ days.

    Note: ``db.get_user_ids_to_remove_active`` only inspects local Discord
    activity. Before stripping the role, we pull
    ``/api/discord/active-pulse`` to see if Interspace still considers
    these users active (e.g. they've been participating only on the web).
    Anyone present in the Interspace pulse is refreshed locally and kept.
    """
    pulse = await _interspace_get(
        f"/api/discord/active-pulse?since=0",
    ) or {}
    web_active_ids = {
        int(u["discordId"]) for u in pulse.get("users", [])
        if u.get("discordId") and str(u["discordId"]).isdigit()
    }

    for guild in bot.guilds:
        role = discord.utils.get(guild.roles, name=ACTIVE_ROLE_NAME)
        if not role:
            continue
        to_remove = await db.get_user_ids_to_remove_active(guild.id)
        for uid in to_remove:
            if uid in web_active_ids:
                # Refresh local timestamp from web-side activity so the user
                # keeps @active.
                await db.record_activity(guild.id, uid)
                continue
            member = guild.get_member(uid)
            if member and role in member.roles:
                try:
                    await member.remove_roles(role, reason="No activity in 7 days")
                except discord.Forbidden:
                    pass


async def _pull_interspace_activity_impl(bot: commands.Bot) -> None:
    """Pull Interspace activity into Discord @active grants.

    Every ``INTERSPACE_PULSE_INTERVAL_MINUTES`` minutes we fetch every
    linked Discord user that has been active on the Interspace in the
    past 7 days and grant them @active locally. This is what enforces
    the user requirement: "users gain the active role by participating
    in the interspace".
    """
    pulse = await _interspace_get("/api/discord/active-pulse?since=0")
    if not pulse:
        return
    for entry in pulse.get("users", []):
        raw_id = entry.get("discordId")
        if not raw_id or not str(raw_id).isdigit():
            continue
        discord_id = int(raw_id)
        for guild in bot.guilds:
            member = guild.get_member(discord_id)
            if not member:
                continue
            await db.record_activity(guild.id, discord_id)
            role = await ensure_active_role(guild)
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Active on Interspace")
                except discord.Forbidden:
                    pass


class ActiveCog(commands.Cog):
    """Assigns @active to users with recent activity. Removes from inactive users."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @tasks.loop(hours=24)
    async def cleanup_stale_active(self) -> None:
        await _remove_stale_active_impl(self.bot)

    @tasks.loop(minutes=INTERSPACE_PULSE_INTERVAL_MINUTES)
    async def pull_interspace_activity(self) -> None:
        await _pull_interspace_activity_impl(self.bot)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.cleanup_stale_active.is_running():
            self.cleanup_stale_active.start()
        if not self.pull_interspace_activity.is_running():
            self.pull_interspace_activity.start()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild and message.author:
            await grant_active_and_record(message.guild, message.author)

    @commands.Cog.listener()
    async def on_reaction_add(
        self,
        reaction: discord.Reaction,
        user: discord.User | discord.Member,
    ) -> None:
        if reaction.message.guild and user:
            await grant_active_and_record(reaction.message.guild, user)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id)
        user = self.bot.get_user(payload.user_id)
        if guild and user:
            await grant_active_and_record(guild, user)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.guild and interaction.user:
            await grant_active_and_record(interaction.guild, interaction.user)
