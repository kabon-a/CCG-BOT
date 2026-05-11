"""Active cog — assign @active role to users with recent activity (within 7 days).

Activity is recorded on two criteria:
  * messages or reactions in the designated courtroom channel
  * admin-approved /record_match submissions

Every grant also pings Interspace via ``/api/discord/active-ping`` so that
linked Interspace users count toward the 65% poll-quorum on the web side.

A periodic task pulls ``/api/discord/active-pulse`` from Interspace, which
returns every linked Discord user who has been active on the *Interspace*
in the past 7 days. We then grant @active to those Discord users, mirroring
their web activity onto Discord — without that, somebody who only ever uses
Interspace would lose @active and be excluded from poll quorums.
"""

from __future__ import annotations

from typing import Dict

import aiohttp
import discord
from discord.ext import commands, tasks

import database as db
from config import INTERSPACE_URL, INTERSPACE_BOT_SECRET

ACTIVE_ROLE_NAME = "active"
COURTROOM_CHANNEL_NAME = "❗❗-the-courtroom"
INTERSPACE_PULSE_INTERVAL_MINUTES = 5

APPROVE_EMOJI = "✅"
REJECT_EMOJI = "❌"


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
        # message_id → { "guild_id": int, "user_id": int, "replay": str }
        self._pending_match_approvals: Dict[int, dict] = {}

    # ── Periodic tasks ──────────────────────────────────────────────────────────

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

    # ── Activity listeners (courtroom-only) ─────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or not message.author:
            return
        if message.channel.name != COURTROOM_CHANNEL_NAME:
            return
        await grant_active_and_record(message.guild, message.author)

    @commands.Cog.listener()
    async def on_reaction_add(
        self,
        reaction: discord.Reaction,
        user: discord.User | discord.Member,
    ) -> None:
        if not reaction.message.guild or not user:
            return
        if reaction.message.channel.name != COURTROOM_CHANNEL_NAME:
            return
        await grant_active_and_record(reaction.message.guild, user)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        # Check if this reaction is an admin approval/rejection for a pending match
        if payload.message_id in self._pending_match_approvals:
            await self._handle_match_reaction(guild, payload)
            return

        # Only grant @active for reactions in the courtroom channel
        channel = guild.get_channel(payload.channel_id)
        if not channel or getattr(channel, 'name', None) != COURTROOM_CHANNEL_NAME:
            return
        user = self.bot.get_user(payload.user_id)
        if user:
            await grant_active_and_record(guild, user)

    # ── /record_match command ────────────────────────────────────────────────────

    @commands.slash_command(
        name="record_match",
        description="Submit a match replay for admin approval. Grants @active on approval.",
    )
    async def record_match(self, ctx: discord.ApplicationContext, replay: str) -> None:
        if not ctx.guild:
            await ctx.respond("This command must be used in a server.", ephemeral=True)
            return

        # Post a pending-approval message that admins can ✅/❌
        embed = discord.Embed(
            title="Match Replay — Pending Approval",
            description=f"**Submitted by:** {ctx.author.mention}\n**Replay:** {replay}",
            colour=discord.Colour.orange(),
        )
        embed.set_footer(text="React ✅ to approve (grants @active) or ❌ to reject.")

        await ctx.respond(embed=embed)
        msg = await ctx.interaction.original_response()

        # Store the pending entry keyed by the bot's reply message ID
        self._pending_match_approvals[msg.id] = {
            "guild_id": ctx.guild.id,
            "user_id": ctx.author.id,
            "replay": replay,
        }

        # Add reaction prompts so admins can one-click approve/reject
        try:
            await msg.add_reaction(APPROVE_EMOJI)
            await msg.add_reaction(REJECT_EMOJI)
        except discord.Forbidden:
            pass

    async def _handle_match_reaction(
        self,
        guild: discord.Guild,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        """Process an admin ✅/❌ reaction on a pending match-approval message."""
        entry = self._pending_match_approvals.get(payload.message_id)
        if not entry:
            return

        # Only admins (members with administrator permission) may approve/reject
        reactor = guild.get_member(payload.user_id)
        if not reactor or reactor.bot:
            return
        if not reactor.guild_permissions.administrator:
            return

        emoji = str(payload.emoji)
        if emoji not in (APPROVE_EMOJI, REJECT_EMOJI):
            return

        # Consume the entry regardless of outcome
        del self._pending_match_approvals[payload.message_id]

        channel = guild.get_channel(payload.channel_id)
        try:
            msg = await channel.fetch_message(payload.message_id)
        except Exception:
            msg = None

        if emoji == APPROVE_EMOJI:
            target = guild.get_member(entry["user_id"])
            if target:
                await grant_active_and_record(guild, target)
            result_text = f"✅ Approved by {reactor.mention}. @active granted to <@{entry['user_id']}>."
        else:
            result_text = f"❌ Rejected by {reactor.mention}."

        if msg:
            try:
                await msg.edit(
                    embed=discord.Embed(
                        title="Match Replay — " + ("Approved" if emoji == APPROVE_EMOJI else "Rejected"),
                        description=msg.embeds[0].description if msg.embeds else "",
                        colour=discord.Colour.green() if emoji == APPROVE_EMOJI else discord.Colour.red(),
                    ).set_footer(text=result_text)
                )
                await msg.clear_reactions()
            except Exception:
                pass
