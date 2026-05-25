"""Active cog — assign @active role to users with recent activity.

A user qualifies for @active by satisfying **either** of these two criteria:

  1. Interacting in the ❗❗-the-courtroom forum channel (posting, replying,
     or reacting in the forum itself or any thread inside it) within the
     last 10 days.
  2. Having a /record_match submission confirmed by an admin within the
     last 7 days.

The role is removed only when *both* windows have expired. A daily cleanup
loop sweeps stale grants.

Outbound mirror: each grant also fires a best-effort ping at Interspace
(``/api/discord/active-ping``) so linked web users count toward the
Interspace-side 65% poll quorum. The outbound ping never decides who gets
@active on Discord — that's strictly governed by the two criteria above.
"""

# NOTE: Do NOT add `from __future__ import annotations` to this file.
# Py-cord introspects slash-command parameter annotations at runtime to build
# option types. With PEP 563 enabled, `replay: str` becomes the *string* "str"
# instead of the class `str`, and py-cord's internal `issubclass(op._raw_type,
# Enum)` then raises `TypeError: issubclass() arg 1 must be a class`, killing
# /record_match with an ApplicationCommandInvokeError.

import time
from typing import Dict

import aiohttp
import discord
from discord.ext import commands, tasks

import database as db
from config import INTERSPACE_URL, INTERSPACE_BOT_SECRET

ACTIVE_ROLE_NAME = "active"
COURTROOM_CHANNEL_NAME = "❗❗-the-courtroom"

APPROVE_EMOJI = "✅"
REJECT_EMOJI = "❌"

# Grant sources — kept as constants so callers don't pass magic strings.
SOURCE_COURTROOM = "courtroom"
SOURCE_MATCH = "match"


def setup(bot: commands.Bot) -> None:
    bot.add_cog(ActiveCog(bot))


# ── Interspace outbound mirror ──────────────────────────────────────────────
#
# Only outbound — we never *pull* @active from Interspace. The two local
# criteria are the sole source of truth for who holds @active on Discord.


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


# ── Channel matching ────────────────────────────────────────────────────────


def _is_courtroom_channel(channel: object) -> bool:
    """True if ``channel`` is the courtroom forum or a thread inside it.

    The courtroom is a Discord forum channel, so user messages live in child
    threads whose ``.name`` is the thread title (not the forum name). We
    match against the thread's parent forum in that case.
    """
    if channel is None:
        return False
    if getattr(channel, "name", None) == COURTROOM_CHANNEL_NAME:
        return True
    parent = getattr(channel, "parent", None)
    if parent is not None and getattr(parent, "name", None) == COURTROOM_CHANNEL_NAME:
        return True
    return False


def _resolve_channel_or_thread(
    guild: discord.Guild, channel_id: int,
) -> object | None:
    """Resolve a raw channel_id to a top-level channel *or* a thread."""
    ch = guild.get_channel(channel_id)
    if ch is not None:
        return ch
    # Forum/text-channel threads aren't returned by get_channel.
    getter = getattr(guild, "get_thread", None)
    if callable(getter):
        return getter(channel_id)
    return None


# ── Role plumbing ───────────────────────────────────────────────────────────


async def ensure_active_role(guild: discord.Guild) -> discord.Role | None:
    """Get or create the @active role. Returns None if bot lacks permissions."""
    role = discord.utils.get(guild.roles, name=ACTIVE_ROLE_NAME)
    if role:
        return role
    try:
        return await guild.create_role(
            name=ACTIVE_ROLE_NAME,
            reason="Active member tracking (courtroom + match policies)",
        )
    except discord.Forbidden:
        return None


async def grant_active(
    guild: discord.Guild,
    user: discord.Member | discord.User,
    *,
    source: str,
) -> None:
    """Stamp activity for the given source and ensure @active is assigned.

    ``source`` must be ``SOURCE_COURTROOM`` or ``SOURCE_MATCH``. Each source
    writes to its own timestamp so the two freshness windows can decay
    independently. Idempotent — safe to call repeatedly.
    """
    if not guild or not user or user.bot:
        return
    member = guild.get_member(user.id) if isinstance(user, discord.User) else user
    if not member:
        return

    if source == SOURCE_COURTROOM:
        await db.record_courtroom_activity(guild.id, user.id)
    elif source == SOURCE_MATCH:
        await db.record_match_activity(guild.id, user.id)
    else:
        return

    role = await ensure_active_role(guild)
    if role and role not in member.roles:
        try:
            await member.add_roles(role, reason=f"Active ({source})")
        except discord.Forbidden:
            pass

    # Outbound mirror: tell Interspace this Discord user just was active so a
    # linked Interspace account is counted toward web-side poll quorum. Never
    # affects the local grant decision; failures are swallowed.
    try:
        await _interspace_post("/api/discord/active-ping", {"discordId": str(user.id)})
    except Exception:
        pass


async def _remove_stale_active_impl(bot: commands.Bot) -> None:
    """Strip @active from users with no activity in either window."""
    for guild in bot.guilds:
        role = discord.utils.get(guild.roles, name=ACTIVE_ROLE_NAME)
        if not role:
            continue
        to_remove = await db.get_user_ids_to_remove_active(guild.id)
        for uid in to_remove:
            member = guild.get_member(uid)
            if member and role in member.roles:
                try:
                    await member.remove_roles(
                        role,
                        reason="No courtroom (10d) or confirmed-match (7d) activity",
                    )
                except discord.Forbidden:
                    pass


class ActiveCog(commands.Cog):
    """Assigns @active to users with recent activity. Removes from inactive users."""

    # Pending match approvals expire after 24 h so the dict never grows unbounded.
    PENDING_APPROVAL_TTL_SECONDS = 86_400

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # message_id → { "guild_id": int, "user_id": int, "replay": str, "created_at": float }
        self._pending_match_approvals: Dict[int, dict] = {}

    # ── Periodic tasks ──────────────────────────────────────────────────────

    @tasks.loop(hours=24)
    async def cleanup_stale_active(self) -> None:
        await _remove_stale_active_impl(self.bot)
        self._prune_stale_match_approvals()

    def _prune_stale_match_approvals(self) -> None:
        """Remove pending match approval entries older than TTL."""
        cutoff = time.time() - self.PENDING_APPROVAL_TTL_SECONDS
        stale = [
            k for k, v in self._pending_match_approvals.items()
            if v.get("created_at", 0) < cutoff
        ]
        for k in stale:
            del self._pending_match_approvals[k]

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.cleanup_stale_active.is_running():
            self.cleanup_stale_active.start()

    # ── Courtroom listeners ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or not message.author or message.author.bot:
            return
        if not _is_courtroom_channel(message.channel):
            return
        await grant_active(message.guild, message.author, source=SOURCE_COURTROOM)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not guild:
            return

        # Admin ✅/❌ on a pending /record_match approval message is handled
        # separately — it doesn't itself count as courtroom activity.
        if payload.message_id in self._pending_match_approvals:
            await self._handle_match_reaction(guild, payload)
            return

        channel = _resolve_channel_or_thread(guild, payload.channel_id)
        if not _is_courtroom_channel(channel):
            return
        user = self.bot.get_user(payload.user_id)
        if user:
            await grant_active(guild, user, source=SOURCE_COURTROOM)

    # ── /record_match command ───────────────────────────────────────────────

    @commands.slash_command(
        name="record_match",
        description="Submit a match replay for admin approval. Grants @active on approval.",
    )
    async def record_match(self, ctx: discord.ApplicationContext, replay: str) -> None:
        if not ctx.guild:
            await ctx.respond("This command must be used in a server.", ephemeral=True)
            return

        # Strip Discord markdown special characters from user-supplied replay
        # string to prevent formatting injection in the embed description.
        safe_replay = (
            (replay or "")
            .replace("`", "\\`")
            .replace("*", "\\*")
            .replace("_", "\\_")
            .replace("~", "\\~")
        )

        embed = discord.Embed(
            title="Match Replay — Pending Approval",
            description=f"**Submitted by:** {ctx.author.mention}\n**Replay:** {safe_replay}",
            colour=discord.Colour.orange(),
        )
        embed.set_footer(text="React ✅ to approve (grants @active) or ❌ to reject.")

        await ctx.respond(embed=embed)
        msg = await ctx.interaction.original_response()

        self._pending_match_approvals[msg.id] = {
            "guild_id": ctx.guild.id,
            "user_id": ctx.author.id,
            "replay": replay,
            "created_at": time.time(),
        }

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

        reactor = guild.get_member(payload.user_id)
        if not reactor or reactor.bot:
            return
        if not reactor.guild_permissions.administrator:
            return

        emoji = str(payload.emoji)
        if emoji not in (APPROVE_EMOJI, REJECT_EMOJI):
            return

        del self._pending_match_approvals[payload.message_id]

        channel = _resolve_channel_or_thread(guild, payload.channel_id)
        try:
            msg = await channel.fetch_message(payload.message_id) if channel else None
        except Exception:
            msg = None

        if emoji == APPROVE_EMOJI:
            target = guild.get_member(entry["user_id"])
            if target:
                await grant_active(guild, target, source=SOURCE_MATCH)
            result_text = (
                f"✅ Approved by {reactor.mention}. "
                f"@active granted to <@{entry['user_id']}>."
            )
        else:
            result_text = f"❌ Rejected by {reactor.mention}."

        if msg:
            try:
                await msg.edit(
                    embed=discord.Embed(
                        title="Match Replay — "
                        + ("Approved" if emoji == APPROVE_EMOJI else "Rejected"),
                        description=msg.embeds[0].description if msg.embeds else "",
                        colour=discord.Colour.green() if emoji == APPROVE_EMOJI else discord.Colour.red(),
                    ).set_footer(text=result_text)
                )
                await msg.clear_reactions()
            except Exception:
                pass
