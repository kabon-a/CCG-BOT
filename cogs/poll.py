"""Poll cog - Courtroom-style polls with Shannon-based winning threshold."""

import json
import math
import re
import discord
from discord import Option
from discord.ext import commands, tasks

import database as db
from cogs.active import grant_active_and_record

POLL_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
EMOJI_TO_INDEX = {e: i for i, e in enumerate(POLL_EMOJIS)}
ACTIVE_ROLE_NAME = "active"
QUORUM_PERCENT = 0.65  # 65% of active eligible must vote


def parse_duration(s: str) -> int | None:
    """Parse duration string like 1d, 24h, 60m into seconds. Returns None if invalid."""
    s = s.strip().lower()
    m = re.match(r"^(\d+)\s*(d|h|m|s)?$", s)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2) or "m"
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return val * multipliers.get(unit, 60)


def compute_n_eff(vote_counts: list[int]) -> float:
    """Exponential Shannon: n_eff = exp(-sum(p_i * ln(p_i))). Skip options with 0 votes."""
    total = sum(vote_counts)
    if total <= 0:
        return 1.0
    entropy = 0.0
    for c in vote_counts:
        if c > 0:
            p = c / total
            entropy += p * math.log(p)
    return math.exp(-entropy)


def compute_pwin(n_eff: float) -> float:
    """Winning vote percentage required: Pwin = 1.5 / (n_eff + 0.5)."""
    return 1.5 / (n_eff + 0.5)


def format_duration(seconds: int) -> str:
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def setup(bot: commands.Bot) -> None:
    bot.add_cog(PollCog(bot))


class PollCog(commands.Cog):
    """Courtroom-style polls with Shannon-based winning threshold."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    poll_group = discord.SlashCommandGroup("poll", "Courtroom-style polls")

    @poll_group.command(name="create", description="Create a poll (custom UI with reactions)")
    async def poll_create(
        self,
        ctx: discord.ApplicationContext,
        title: Option(str, "Poll title", required=True),
        options: Option(str, "Comma-separated options (e.g. Yes, No, Abstain)", required=True),
        duration: Option(str, "Duration: 1d, 24h, 60m, etc.", required=True),
        roles: Option(
            str,
            "Role names that can vote, comma-separated. Leave empty for everyone.",
            required=False,
        ) = None,
    ) -> None:
        if not ctx.guild or not ctx.author:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return

        opts = [o.strip() for o in options.split(",") if o.strip()]
        if len(opts) < 2:
            await ctx.respond("Provide at least 2 options.", ephemeral=True)
            return
        if len(opts) > len(POLL_EMOJIS):
            await ctx.respond(f"Maximum {len(POLL_EMOJIS)} options supported.", ephemeral=True)
            return

        dur_sec = parse_duration(duration)
        if not dur_sec or dur_sec < 60:
            await ctx.respond("Invalid duration. Use e.g. 1d, 24h, 60m (minimum 1 minute).", ephemeral=True)
            return

        role_ids: list[int] = []
        if roles and roles.strip():
            for rname in [r.strip() for r in roles.split(",") if r.strip()]:
                role = discord.utils.get(ctx.guild.roles, name=rname)
                if role:
                    role_ids.append(role.id)
                else:
                    await ctx.respond(f"Role **{rname}** not found.", ephemeral=True)
                    return

        lines = []
        for i, opt in enumerate(opts):
            lines.append(f"{POLL_EMOJIS[i]} {opt}")
        desc = "\n".join(lines)
        embed = discord.Embed(
            title=f"📋 {title}",
            description=desc,
            color=0x2E86AB,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Closes in {format_duration(dur_sec)} • React to vote (multiple allowed)")
        msg = await ctx.channel.send(embed=embed)

        for i in range(len(opts)):
            await msg.add_reaction(POLL_EMOJIS[i])

        poll_id = await db.create_poll(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            message_id=msg.id,
            title=title,
            options=opts,
            role_ids=role_ids,
            duration_seconds=dur_sec,
        )
        await ctx.respond(f"Poll created. ID: {poll_id}", ephemeral=True)

    @poll_group.command(name="delete", description="Remove a poll from the bot (Mod/Admin). Optionally delete the message.")
    async def poll_delete(
        self,
        ctx: discord.ApplicationContext,
        poll_id: Option(int, "Poll ID (shown when the poll was created)", required=True),
        delete_message: Option(
            bool,
            "Try to delete the poll message in Discord (default: true)",
            required=False,
        ) = True,
    ) -> None:
        if not ctx.guild or not ctx.author:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        perms = ctx.author.guild_permissions
        if not (perms.administrator or perms.manage_guild or perms.manage_messages):
            await ctx.respond("You need Administrator, Manage Server, or Manage Messages permission.", ephemeral=True)
            return

        poll = await db.get_poll_by_id(poll_id)
        if not poll or poll["guild_id"] != ctx.guild.id:
            await ctx.respond(f"No poll with ID **{poll_id}** in this server.", ephemeral=True)
            return

        if delete_message:
            channel = ctx.guild.get_channel(poll["channel_id"])
            if channel and isinstance(channel, discord.TextChannel):
                try:
                    msg = await channel.fetch_message(poll["message_id"])
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        await db.delete_poll(poll_id)
        await ctx.respond(f"Poll **{poll_id}** removed from the bot.", ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == self.bot.user.id:
            return
        await self._handle_reaction(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload, add=False)

    async def _handle_reaction(
        self,
        payload: discord.RawReactionActionEvent,
        *,
        add: bool,
    ) -> None:
        emoji_key = str(payload.emoji)
        if emoji_key not in EMOJI_TO_INDEX:
            return
        option_index = EMOJI_TO_INDEX[emoji_key]
        poll = await db.get_poll_by_message(payload.guild_id or 0, payload.message_id)
        if not poll:
            return
        if add:
            await db.add_poll_vote(poll["id"], payload.user_id, option_index)
            guild = self.bot.get_guild(payload.guild_id)
            user = self.bot.get_user(payload.user_id)
            if guild and user:
                await grant_active_and_record(guild, user)
        else:
            await db.remove_poll_vote(poll["id"], payload.user_id, option_index)

    @tasks.loop(seconds=30)
    async def check_poll_closures(self) -> None:
        """Check for ended polls and post the courtroom report."""
        pending = await db.get_pending_polls()
        for poll in pending:
            await self._close_poll(poll)
            await db.delete_poll(poll["id"])

    async def _close_poll(self, poll: dict) -> None:
        guild = self.bot.get_guild(poll["guild_id"])
        if not guild:
            return
        channel = guild.get_channel(poll["channel_id"])
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        options = json.loads(poll["options"])
        role_ids: list[int] = json.loads(poll["role_ids"]) if poll["role_ids"] else []

        active_role = discord.utils.get(guild.roles, name=ACTIVE_ROLE_NAME)
        if active_role:
            active_member_ids = {m.id for m in active_role.members}
        else:
            active_member_ids = await db.get_active_user_ids(guild.id)

        def is_eligible(m: discord.Member) -> bool:
            if m.bot:
                return False
            if not role_ids:
                return True
            return any(r.id in role_ids for r in m.roles)

        active_eligible = [
            m for m in guild.members
            if is_eligible(m) and m.id in active_member_ids
        ]
        num_active_eligible = len(active_eligible)
        active_eligible_ids = {m.id for m in active_eligible}

        votes = await db.get_poll_votes(poll["id"])
        # Only count votes from active eligible voters
        valid_votes = [(uid, oi) for uid, oi in votes if uid in active_eligible_ids]
        valid_voter_ids = {uid for uid, _ in valid_votes}
        num_valid_voters = len(valid_voter_ids)
        total_valid_votes = len(valid_votes)

        if num_active_eligible == 0:
            pct_eligible_voted = 0.0
        else:
            pct_eligible_voted = 100.0 * num_valid_voters / num_active_eligible

        vote_counts = [0] * len(options)
        for _, opt_idx in valid_votes:
            if 0 <= opt_idx < len(options):
                vote_counts[opt_idx] += 1

        n_eff = compute_n_eff(vote_counts)
        pwin_required = compute_pwin(n_eff)

        if total_valid_votes > 0:
            max_count = max(vote_counts)
            winning_idx = vote_counts.index(max_count)
            winning_label = options[winning_idx]
            winning_pct = 100.0 * max_count / total_valid_votes
            passed = winning_pct >= (pwin_required * 100)
        else:
            winning_label = "N/A"
            winning_pct = 0.0
            passed = False

        quorum_met = num_valid_voters >= (QUORUM_PERCENT * num_active_eligible)
        if not quorum_met:
            passed = False

        result_lines = [
            f"**1. No. of Active Eligible Voters:** {num_active_eligible}",
            f"**2. Total Number of Valid Voters:** {num_valid_voters}",
            f"**3. Total Number of Valid Votes:** {total_valid_votes}",
            f"**4. Percentage of Eligible Voters that Voted:** {pct_eligible_voted:.1f}%",
            f"**5. Winning Vote Percentage Required:** {pwin_required * 100:.1f}%",
            f"**6. Winning Vote Percentage Acquired:** {winning_pct:.1f}% ({winning_label})",
            "",
        ]
        if passed:
            result_lines.append("✅ **The winning vote passed.**")
        else:
            result_lines.append("❌ **The winning vote did not pass. The proposal is adjourned.**")
            if not quorum_met:
                result_lines.append("*(Quorum not met: fewer than 65% of active eligible voters participated.)*")

        embed = discord.Embed(
            title=f"📊 Poll Results: {poll['title']}",
            description="\n".join(result_lines),
            color=0x2E86AB if passed else 0x8B0000,
            timestamp=discord.utils.utcnow(),
        )
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.check_poll_closures.is_running():
            self.check_poll_closures.start()
