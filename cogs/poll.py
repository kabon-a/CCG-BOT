"""Poll cog - Courtroom voting system with Simpson-based thresholds."""

import json
import math
import re
from statistics import mean

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
    """Inverse Simpson effective options: n_eff = 1 / sum(p_i^2)."""
    total = sum(vote_counts)
    if total <= 0:
        return 1.0
    simpson_d = 0.0
    for c in vote_counts:
        if c > 0:
            p = c / total
            simpson_d += p * p
    if simpson_d <= 0:
        return 1.0
    return 1.0 / simpson_d


def compute_pwin(n_eff: float) -> float:
    """Winning vote percentage required: Pwin = 1.5 / (n_eff + 0.5)."""
    return 1.5 / (n_eff + 0.5)


def compute_option_pwin_from_tiers(tier_counts: list[int]) -> tuple[float, float]:
    """Return (n_eff, p_win) for an option's tier distribution."""
    n_eff = compute_n_eff(tier_counts)
    return n_eff, compute_pwin(n_eff)


def expected_value_and_ci_90(tier_counts: list[int]) -> tuple[float, float, float]:
    """Return (ev, ci_low, ci_high)."""
    n = sum(tier_counts)
    if n <= 0:
        return (0.0, 0.0, 0.0)
    values: list[int] = []
    for idx, c in enumerate(tier_counts, 1):
        values.extend([idx] * c)
    ev = mean(values)
    var = sum((v - ev) ** 2 for v in values) / n
    sigma = math.sqrt(var)
    se = sigma / math.sqrt(n)
    margin = 1.645 * se
    return (ev, ev - margin, ev + margin)


def ci_to_tier(ci_low: float, ci_high: float, num_tiers: int) -> int | None:
    """Accept assignment only if full CI lies inside one tier bucket."""
    for t in range(1, num_tiers + 1):
        lower = float("-inf") if t == 1 else (t - 0.5)
        upper = float("inf") if t == num_tiers else (t + 0.5)
        if ci_low > lower and ci_high <= upper:
            return t
        if t == 1 and ci_high <= upper:
            return t
    return None


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
    """Courtroom-style polls with Simpson-based thresholds."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    poll_group = discord.SlashCommandGroup("poll", "Courtroom-style polls")

    async def _get_active_eligible_ids(self, guild: discord.Guild, role_ids: list[int]) -> tuple[int, set[int]]:
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

        active_eligible = [m for m in guild.members if is_eligible(m) and m.id in active_member_ids]
        return len(active_eligible), {m.id for m in active_eligible}

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

        lines = [f"{POLL_EMOJIS[i]} {opt}" for i, opt in enumerate(opts)]
        embed = discord.Embed(
            title=f"📋 {title}",
            description="\n".join(lines),
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

    @poll_group.command(name="stage_create", description="Create a two-stage tier poll (Simpson + EV fallback).")
    async def stage_create(
        self,
        ctx: discord.ApplicationContext,
        title: Option(str, "Proposal title", required=True),
        options: Option(str, "Comma-separated options", required=True),
        duration: Option(str, "Stage 1 duration (e.g. 1d, 24h, 60m)", required=True),
        num_tiers: Option(int, "Number of tiers allowed (n tiers)", required=True),
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
        if num_tiers < 2:
            await ctx.respond("num_tiers must be at least 2.", ephemeral=True)
            return
        dur_sec = parse_duration(duration)
        if not dur_sec or dur_sec < 60:
            await ctx.respond("Invalid duration. Minimum is 1 minute.", ephemeral=True)
            return
        role_ids: list[int] = []
        if roles and roles.strip():
            for rname in [r.strip() for r in roles.split(",") if r.strip()]:
                role = discord.utils.get(ctx.guild.roles, name=rname)
                if not role:
                    await ctx.respond(f"Role **{rname}** not found.", ephemeral=True)
                    return
                role_ids.append(role.id)
        poll_id = await db.create_stage_poll(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            title=title,
            options=opts,
            role_ids=role_ids,
            num_tiers=num_tiers,
            duration_seconds=dur_sec,
        )
        opt_lines = "\n".join([f"`{i+1}`. {opt}" for i, opt in enumerate(opts)])
        embed = discord.Embed(
            title=f"⚖️ Stage 1 Open: {title}",
            description=(
                f"**Poll ID:** `{poll_id}`\n"
                f"**Tier range:** 1 to {num_tiers} (1 = most balanced)\n\n"
                f"**Options:**\n{opt_lines}\n\n"
                "Vote with `/poll stage_vote poll_id:<id> option_index:<n> tier:<n>`."
            ),
            color=0x345995,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Stage 1 closes in {format_duration(dur_sec)}")
        await ctx.channel.send(embed=embed)
        await ctx.respond(f"Two-stage poll created with ID `{poll_id}`.", ephemeral=True)

    @poll_group.command(name="stage_vote", description="Submit/replace your Stage 1 tier input for an option.")
    async def stage_vote(
        self,
        ctx: discord.ApplicationContext,
        poll_id: Option(int, "Stage poll ID", required=True),
        option_index: Option(int, "Option number (1-based)", required=True),
        tier: Option(int, "Tier value (1..num_tiers)", required=True),
    ) -> None:
        if not ctx.guild or not ctx.author:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        poll = await db.get_stage_poll_by_id(poll_id)
        if not poll or poll["guild_id"] != ctx.guild.id:
            await ctx.respond("Stage poll not found in this server.", ephemeral=True)
            return
        if poll["status"] != "stage1_open":
            await ctx.respond("Stage 1 is not open for this poll.", ephemeral=True)
            return
        options = json.loads(poll["options"])
        if option_index < 1 or option_index > len(options):
            await ctx.respond(f"option_index must be between 1 and {len(options)}.", ephemeral=True)
            return
        if tier < 1 or tier > int(poll["num_tiers"]):
            await ctx.respond(f"tier must be between 1 and {poll['num_tiers']}.", ephemeral=True)
            return
        role_ids: list[int] = json.loads(poll["role_ids"]) if poll["role_ids"] else []
        if role_ids and not any(r.id in role_ids for r in ctx.author.roles):
            await ctx.respond("You are not eligible to vote on this poll.", ephemeral=True)
            return
        await db.add_stage_vote(poll_id, ctx.author.id, option_index - 1, tier)
        await grant_active_and_record(ctx.guild, ctx.author)
        await ctx.respond(
            f"Recorded: option `{option_index}` ({options[option_index-1]}) -> tier `{tier}`.",
            ephemeral=True,
        )

    @poll_group.command(name="stage_close", description="Close Stage 1 and compute assignments/outcome.")
    async def stage_close(
        self,
        ctx: discord.ApplicationContext,
        poll_id: Option(int, "Stage poll ID", required=True),
    ) -> None:
        if not ctx.guild or not ctx.author:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        perms = ctx.author.guild_permissions
        if not (perms.administrator or perms.manage_guild or perms.manage_messages):
            await ctx.respond("You need Administrator, Manage Server, or Manage Messages permission.", ephemeral=True)
            return
        poll = await db.get_stage_poll_by_id(poll_id)
        if not poll or poll["guild_id"] != ctx.guild.id:
            await ctx.respond("Stage poll not found.", ephemeral=True)
            return
        if poll["status"] != "stage1_open":
            await ctx.respond("Stage 1 is already closed for this poll.", ephemeral=True)
            return

        guild = ctx.guild
        options: list[str] = json.loads(poll["options"])
        role_ids: list[int] = json.loads(poll["role_ids"]) if poll["role_ids"] else []
        num_tiers = int(poll["num_tiers"])
        _, eligible_ids = await self._get_active_eligible_ids(guild, role_ids)
        votes = await db.get_stage_votes(poll_id)
        valid_votes = [(u, oi, t) for (u, oi, t) in votes if u in eligible_ids]

        assignments: list[int | None] = []
        report_lines: list[str] = []

        for oi, opt in enumerate(options):
            tier_counts = [0] * num_tiers
            option_tiers: list[int] = []
            for _, vote_opt, tier in valid_votes:
                if vote_opt == oi and 1 <= tier <= num_tiers:
                    tier_counts[tier - 1] += 1
                    option_tiers.append(tier)
            total_opt_votes = len(option_tiers)
            if total_opt_votes == 0:
                assignments.append(None)
                report_lines.append(f"**{oi+1}. {opt}** — inconclusive (no votes)")
                continue
            mode_count = max(tier_counts)
            mode_tier = tier_counts.index(mode_count) + 1
            mode_freq = mode_count / total_opt_votes
            n_eff, p_win = compute_option_pwin_from_tiers(tier_counts)
            if mode_freq >= p_win:
                assignments.append(mode_tier)
                report_lines.append(
                    f"**{oi+1}. {opt}** — Tier {mode_tier} via Simpson threshold "
                    f"(mode {mode_freq*100:.1f}% >= {p_win*100:.1f}%, n_eff={n_eff:.3f})"
                )
            else:
                ev, lo, hi = expected_value_and_ci_90(tier_counts)
                assigned = ci_to_tier(lo, hi, num_tiers)
                if assigned is None:
                    assignments.append(None)
                    report_lines.append(
                        f"**{oi+1}. {opt}** — inconclusive after fallback "
                        f"(E={ev:.3f}, CI90=({lo:.3f}, {hi:.3f}))"
                    )
                else:
                    assignments.append(assigned)
                    report_lines.append(
                        f"**{oi+1}. {opt}** — Tier {assigned} via EV fallback "
                        f"(E={ev:.3f}, CI90=({lo:.3f}, {hi:.3f}))"
                    )

        t1_idxs = [i for i, t in enumerate(assignments) if t == 1]
        assigned_only = [t for t in assignments if t is not None]

        if not assigned_only:
            attempts = int(poll["attempts"]) + 1
            new_status = "annulled" if attempts >= 3 else "failed_stage1"
            await db.set_stage_poll_status(poll_id, new_status, attempts=attempts)
            outcome = (
                "❌ Stage 1 failed entirely. "
                + ("Proposal annulled after 3 failures." if attempts >= 3 else "Proposal should be rescheduled.")
            )
        elif len(t1_idxs) >= 2:
            await db.set_stage_poll_status(poll_id, "preference_open", preference_options=t1_idxs)
            labels = ", ".join([f"`{i+1}` {options[i]}" for i in t1_idxs])
            outcome = (
                "🗳️ Stage 2 preference opened (Tier 1 tie). "
                f"Eligible options: {labels}. Use `/poll preference_vote` then `/poll preference_close`."
            )
        elif len(t1_idxs) == 1:
            winner = t1_idxs[0]
            await db.set_stage_poll_status(poll_id, "passed_auto")
            outcome = f"✅ Auto-selected option `{winner+1}` ({options[winner]}) — only Tier 1 option."
        else:
            min_tier = min(assigned_only)
            min_idxs = [i for i, t in enumerate(assignments) if t == min_tier]
            if len(set(assigned_only)) == 1:
                await db.set_stage_poll_status(poll_id, "preference_open", preference_options=min_idxs)
                labels = ", ".join([f"`{i+1}` {options[i]}" for i in min_idxs])
                outcome = (
                    "🗳️ Stage 2 preference opened (all assigned same tier). "
                    f"Eligible options: {labels}. Use `/poll preference_vote` then `/poll preference_close`."
                )
            elif len(min_idxs) == 1:
                winner = min_idxs[0]
                await db.set_stage_poll_status(poll_id, "passed_auto")
                outcome = f"✅ Auto-selected option `{winner+1}` ({options[winner]}) — lowest assigned tier."
            else:
                await db.set_stage_poll_status(poll_id, "preference_open", preference_options=min_idxs)
                labels = ", ".join([f"`{i+1}` {options[i]}" for i in min_idxs])
                outcome = (
                    "🗳️ Stage 2 preference opened (tie at best assigned tier). "
                    f"Eligible options: {labels}. Use `/poll preference_vote` then `/poll preference_close`."
                )

        embed = discord.Embed(
            title=f"📊 Stage 1 Results: {poll['title']} (ID {poll_id})",
            description="\n".join(report_lines + ["", outcome]),
            color=0x2E86AB,
            timestamp=discord.utils.utcnow(),
        )
        await ctx.channel.send(embed=embed)
        await ctx.respond("Stage 1 closed and report posted.", ephemeral=True)

    @poll_group.command(name="preference_vote", description="Vote in Stage 2 preference round.")
    async def preference_vote(
        self,
        ctx: discord.ApplicationContext,
        poll_id: Option(int, "Stage poll ID", required=True),
        option_index: Option(int, "Option number (1-based)", required=True),
    ) -> None:
        if not ctx.guild or not ctx.author:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        poll = await db.get_stage_poll_by_id(poll_id)
        if not poll or poll["guild_id"] != ctx.guild.id:
            await ctx.respond("Stage poll not found.", ephemeral=True)
            return
        if poll["status"] != "preference_open":
            await ctx.respond("Preference stage is not open for this poll.", ephemeral=True)
            return
        options = json.loads(poll["options"])
        pref_opts = json.loads(poll["preference_options"]) if poll["preference_options"] else []
        oi = option_index - 1
        if oi not in pref_opts:
            allowed = ", ".join(str(i + 1) for i in pref_opts)
            await ctx.respond(f"Option must be one of: {allowed}.", ephemeral=True)
            return
        role_ids: list[int] = json.loads(poll["role_ids"]) if poll["role_ids"] else []
        if role_ids and not any(r.id in role_ids for r in ctx.author.roles):
            await ctx.respond("You are not eligible to vote on this poll.", ephemeral=True)
            return
        await db.add_stage_preference_vote(poll_id, ctx.author.id, oi)
        await grant_active_and_record(ctx.guild, ctx.author)
        await ctx.respond(f"Preference vote recorded for option `{option_index}` ({options[oi]}).", ephemeral=True)

    @poll_group.command(name="preference_close", description="Close Stage 2 preference and determine result.")
    async def preference_close(
        self,
        ctx: discord.ApplicationContext,
        poll_id: Option(int, "Stage poll ID", required=True),
    ) -> None:
        if not ctx.guild or not ctx.author:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        perms = ctx.author.guild_permissions
        if not (perms.administrator or perms.manage_guild or perms.manage_messages):
            await ctx.respond("You need Administrator, Manage Server, or Manage Messages permission.", ephemeral=True)
            return
        poll = await db.get_stage_poll_by_id(poll_id)
        if not poll or poll["guild_id"] != ctx.guild.id:
            await ctx.respond("Stage poll not found.", ephemeral=True)
            return
        if poll["status"] != "preference_open":
            await ctx.respond("Preference stage is not open for this poll.", ephemeral=True)
            return

        options = json.loads(poll["options"])
        pref_opts = json.loads(poll["preference_options"]) if poll["preference_options"] else []
        role_ids: list[int] = json.loads(poll["role_ids"]) if poll["role_ids"] else []
        num_active_eligible, active_eligible_ids = await self._get_active_eligible_ids(ctx.guild, role_ids)
        votes = await db.get_stage_preference_votes(poll_id)
        valid_votes = [(u, oi) for (u, oi) in votes if u in active_eligible_ids and oi in pref_opts]
        num_valid_voters = len({u for u, _ in valid_votes})

        counts = [0] * len(options)
        for _, oi in valid_votes:
            counts[oi] += 1
        scoped_counts = [counts[i] for i in pref_opts]
        total_valid_votes = sum(scoped_counts)
        n_eff = compute_n_eff(scoped_counts)
        p_win = compute_pwin(n_eff)
        quorum_met = num_valid_voters >= (QUORUM_PERCENT * num_active_eligible)

        if total_valid_votes > 0:
            local_max = max(scoped_counts)
            local_idx = scoped_counts.index(local_max)
            winner_idx = pref_opts[local_idx]
            win_pct = local_max / total_valid_votes
        else:
            winner_idx = None
            win_pct = 0.0

        passed = quorum_met and winner_idx is not None and win_pct >= p_win
        attempts = int(poll["attempts"])
        if passed:
            await db.set_stage_poll_status(poll_id, "passed_preference")
            outcome = f"✅ Preference passed. Winner: `{winner_idx+1}` ({options[winner_idx]})."
        else:
            attempts += 1
            new_status = "annulled" if attempts >= 3 else "failed_preference"
            await db.set_stage_poll_status(poll_id, new_status, attempts=attempts)
            outcome = (
                "❌ Preference did not pass."
                + (" Proposal annulled after 3 failures." if attempts >= 3 else " Proposal should be rescheduled.")
            )
            if not quorum_met:
                outcome += " (Quorum not met.)"

        lines = [
            f"**Active Eligible Voters:** {num_active_eligible}",
            f"**Valid Voters:** {num_valid_voters}",
            f"**Valid Votes:** {total_valid_votes}",
            f"**Simpson n_eff:** {n_eff:.3f}",
            f"**Required p_win:** {p_win*100:.1f}%",
            f"**Winner share:** {win_pct*100:.1f}%",
            "",
            outcome,
        ]
        embed = discord.Embed(
            title=f"🗳️ Stage 2 Preference Result: {poll['title']} (ID {poll_id})",
            description="\n".join(lines),
            color=0x2E86AB if passed else 0x8B0000,
            timestamp=discord.utils.utcnow(),
        )
        await ctx.channel.send(embed=embed)
        await ctx.respond("Preference stage closed and result posted.", ephemeral=True)
        await db.clear_stage_preference_votes(poll_id)

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
        num_active_eligible, active_eligible_ids = await self._get_active_eligible_ids(guild, role_ids)

        votes = await db.get_poll_votes(poll["id"])
        valid_votes = [(uid, oi) for uid, oi in votes if uid in active_eligible_ids]
        valid_voter_ids = {uid for uid, _ in valid_votes}
        num_valid_voters = len(valid_voter_ids)
        total_valid_votes = len(valid_votes)
        pct_eligible_voted = 0.0 if num_active_eligible == 0 else (100.0 * num_valid_voters / num_active_eligible)

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
            f"**5. Winning Vote Percentage Required (Simpson):** {pwin_required * 100:.1f}% (n_eff={n_eff:.3f})",
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
