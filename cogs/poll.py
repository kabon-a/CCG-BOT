"""Poll cog — Courtroom voting via Interspace web UI.

Stage 1 tier assignments are submitted through the Interspace frontend.
This cog handles:
  - /poll stage_create  → registers the poll with Interspace, posts announcement.
                          Now takes a single proposal_id (the PROP-XXXX from
                          Interspace) and derives title/text/type/tier from it.
  - /poll stage_close   → manually triggers result computation on Interspace.
  - /poll status        → shows current poll results.
  - /poll delete        → removes a poll.
  - /poll preference_create → unchanged simple reaction poll (no Interspace).
  - Background task     → auto-closes expired polls; fetches results from Interspace.

Petition Hall:
  When Interspace approves a proposal it can be re-posted by the bot to the
  configured channel "🏰-the-petition-hall" using `post_to_petition_hall`.

Removed: /poll stage_vote (voting is now done on the Interspace web UI).
Removed parameters from stage_create: proposal_type, proposal_text, roles
  (role gating now derives from the proposal tier on the Interspace side).
"""

import json
import math
import re
from statistics import mean

import aiohttp
import discord
from discord import Option
from discord.ext import commands, tasks

import database as db
from cogs.active import grant_active_and_record
from config import INTERSPACE_URL, INTERSPACE_BOT_SECRET

POLL_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
EMOJI_TO_INDEX = {e: i for i, e in enumerate(POLL_EMOJIS)}
ACTIVE_ROLE_NAME = "active"
QUORUM_PERCENT = 0.65
PETITION_HALL_CHANNEL_NAME = "🏰-the-petition-hall"


def parse_duration(s: str) -> int | None:
    s = s.strip().lower()
    m = re.match(r"^(\d+)\s*(d|h|m|s)?$", s)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2) or "m"
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return val * multipliers.get(unit, 60)


def compute_n_eff(vote_counts: list[int]) -> float:
    total = sum(vote_counts)
    if total <= 0:
        return 1.0
    simpson_d = sum((c / total) ** 2 for c in vote_counts if c > 0)
    return 1.0 / simpson_d if simpson_d > 0 else 1.0


def compute_pwin(n_eff: float) -> float:
    return 1.5 / (n_eff + 0.5)


def format_duration(seconds: int) -> str:
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _interspace_headers() -> dict:
    return {"x-bot-secret": INTERSPACE_BOT_SECRET, "Content-Type": "application/json"}


async def _interspace_post(path: str, payload: dict) -> dict | None:
    """POST to Interspace backend. Returns parsed JSON or None on failure."""
    if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
        return None
    url = f"{INTERSPACE_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=_interspace_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                text = await resp.text()
                print(f"[Interspace] POST {path} → {resp.status}: {text[:200]}")
                return None
    except Exception as exc:
        print(f"[Interspace] POST {path} failed: {exc}")
        return None


async def _interspace_get(path: str) -> dict | None:
    """GET from Interspace backend. Returns parsed JSON or None on failure."""
    if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
        return None
    url = f"{INTERSPACE_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_interspace_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                print(f"[Interspace] GET {path} → {resp.status}: {text[:200]}")
                return None
    except Exception as exc:
        print(f"[Interspace] GET {path} failed: {exc}")
        return None


def _format_petition_hall_post(proposal: dict) -> str:
    """Build the petition-hall message in the requested format.

        [Proposal Type] & [Proposal Tier]
        [Proposal Title]: [Proposal ID]
        [Proposal Content]
    """
    ptype = (proposal.get("proposalType") or "format").replace("_", " ").title()
    tier = proposal.get("tier")
    sub = proposal.get("tier3SubType")
    if tier == 3 and sub:
        tier_str = f"Tier 3 (Type {sub})"
    elif tier:
        tier_str = f"Tier {tier}"
    else:
        tier_str = "Untiered"
    title = proposal.get("title") or ""
    short_id = proposal.get("proposalId") or proposal.get("id") or ""
    content = proposal.get("proposalText") or ""
    return (
        f"```\n"
        f"{ptype} & {tier_str}\n\n"
        f"{title}: {short_id}\n\n"
        f"{content}\n"
        f"```"
    )


async def post_to_petition_hall(guild: discord.Guild, proposal: dict) -> discord.Message | None:
    """Post an approved proposal to '🏰-the-petition-hall' if the channel exists."""
    if not guild:
        return None
    channel = discord.utils.get(guild.text_channels, name=PETITION_HALL_CHANNEL_NAME)
    if channel is None:
        # Try a looser match — Discord normalizes some emoji + dash names.
        channel = next((c for c in guild.text_channels
                        if c.name.replace("_", "-") == PETITION_HALL_CHANNEL_NAME.replace("_", "-")), None)
    if channel is None:
        print(f"[PetitionHall] Channel '{PETITION_HALL_CHANNEL_NAME}' not found in guild {guild.id}")
        return None
    try:
        return await channel.send(_format_petition_hall_post(proposal))
    except discord.HTTPException as exc:
        print(f"[PetitionHall] Failed to post: {exc}")
        return None


async def _interspace_post_compute(path: str) -> dict | None:
    """POST (no body) to Interspace compute endpoint."""
    if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
        return None
    url = f"{INTERSPACE_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=_interspace_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                text = await resp.text()
                print(f"[Interspace] POST {path} → {resp.status}: {text[:200]}")
                return None
    except Exception as exc:
        print(f"[Interspace] POST {path} failed: {exc}")
        return None


def setup(bot: commands.Bot) -> None:
    bot.add_cog(PollCog(bot))


class PollCog(commands.Cog):
    """Courtroom-style polls with Interspace web voting."""

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

    async def _build_regular_status_embed(self, guild: discord.Guild, poll: dict) -> discord.Embed:
        now = discord.utils.utcnow().timestamp()
        options = json.loads(poll["options"])
        votes = await db.get_poll_votes(poll["id"])
        counts = [0] * len(options)
        unique_voters: set = set()
        for uid, oi in votes:
            unique_voters.add(uid)
            if 0 <= oi < len(options):
                counts[oi] += 1
        total_votes = sum(counts)
        n_eff = compute_n_eff(counts)
        pwin_required = compute_pwin(n_eff) * 100.0
        closes_in = max(0, int(float(poll["ends_at"]) - now))
        is_open = closes_in > 0

        lines = []
        for i, opt in enumerate(options):
            c = counts[i]
            pct = 0.0 if total_votes == 0 else (100.0 * c / total_votes)
            lines.append(f"{POLL_EMOJIS[i]} **{opt}** - {c} vote(s) ({pct:.1f}%)")

        desc = "\n".join(lines) if lines else "*No options.*"
        desc += (
            f"\n\n**Status:** {'Open' if is_open else 'Closed/Pending finalize'}"
            f"\n**Unique voters:** {len(unique_voters)}"
            f"\n**Total votes:** {total_votes}"
            f"\n**Simpson threshold now:** {pwin_required:.1f}% (n_eff={n_eff:.3f})"
        )
        if is_open:
            desc += f"\n**Closes in:** {format_duration(closes_in)}"

        return discord.Embed(
            title=f"Poll Status: {poll['title']} (ID {poll['id']})",
            description=desc,
            color=0x2E86AB,
            timestamp=discord.utils.utcnow(),
        )

    async def _build_stage_status_embed(self, stage: dict) -> discord.Embed:
        now = discord.utils.utcnow().timestamp()
        poll_id = int(stage["id"])
        options = json.loads(stage["options"])
        num_tiers = int(stage["num_tiers"])
        status = str(stage["status"])
        votes = await db.get_stage_votes(poll_id)

        by_option = [[0] * num_tiers for _ in options]
        unique_voters: set = set()
        for uid, oi, tier in votes:
            unique_voters.add(uid)
            if 0 <= oi < len(options) and 1 <= tier <= num_tiers:
                by_option[oi][tier - 1] += 1

        lines = [f"**Status:** `{status}`", f"**Stage 1 unique voters (Interspace):** {len(unique_voters)}"]
        stage1_left = max(0, int(float(stage["ends_at"]) - now))
        if status == "stage1_open":
            lines.append(f"**Stage 1 closes in:** {format_duration(stage1_left)}")

        lines.append("")
        lines.append("**Voting is done via the Interspace web UI.**")
        if INTERSPACE_URL:
            lines.append(f"🔗 {INTERSPACE_URL}")

        return discord.Embed(
            title=f"Stage Poll Status: {stage['title']} (ID {poll_id})",
            description="\n".join(lines),
            color=0x345995,
            timestamp=discord.utils.utcnow(),
        )

    async def _refresh_live_status_messages(self, guild_id: int, kind: str, poll_id: int) -> None:
        rows = await db.get_live_poll_statuses(guild_id, kind, poll_id)
        if not rows:
            return
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        if kind == "regular":
            poll = await db.get_poll_by_id(poll_id)
            if not poll or int(poll["guild_id"]) != guild_id:
                for row_id, _, _ in rows:
                    await db.delete_live_poll_status_row(row_id)
                return
            embed = await self._build_regular_status_embed(guild, poll)
        else:
            stage = await db.get_stage_poll_by_id(poll_id)
            if not stage or int(stage["guild_id"]) != guild_id:
                for row_id, _, _ in rows:
                    await db.delete_live_poll_status_row(row_id)
                return
            embed = await self._build_stage_status_embed(stage)

        for row_id, channel_id, message_id in rows:
            channel = guild.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                await db.delete_live_poll_status_row(row_id)
                continue
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(embed=embed)
            except discord.NotFound:
                await db.delete_live_poll_status_row(row_id)
            except discord.HTTPException:
                pass

    @poll_group.command(name="preference_create", description="Create a preference poll (reaction voting).")
    async def poll_create(
        self,
        ctx: discord.ApplicationContext,
        title: Option(str, "Poll title", required=True),
        options: Option(str, "Comma-separated options", required=True),
        duration: Option(str, "Duration: 1d, 24h, 60m, etc.", required=True),
        roles: Option(str, "Role names that can vote, comma-separated. Empty = everyone.", required=False) = None,
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
        embed.set_footer(text=f"Closes in {format_duration(dur_sec)} • React to vote")
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

    @poll_group.command(name="stage_create", description="Create a two-stage tier poll bound to an Interspace proposal.")
    async def stage_create(
        self,
        ctx: discord.ApplicationContext,
        proposal_id: Option(str, "Interspace proposal ID (e.g. PROP-A4F2)", required=True),
        options: Option(str, "Comma-separated options", required=True),
        duration: Option(str, "Stage 1 duration (e.g. 1d, 24h, 60m)", required=True),
        preference_duration: Option(str, "Stage 2 preference duration if needed", required=True),
        num_tiers: Option(int, "Number of tiers (n tiers)", required=True),
    ) -> None:
        """Open a two-stage tier poll for an approved proposal.

        Title, content, type and voter-role gating are derived from the
        proposal on the Interspace side — no need to re-enter them.
        """
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
        pref_dur_sec = parse_duration(preference_duration)
        if not pref_dur_sec or pref_dur_sec < 60:
            await ctx.respond("Invalid preference_duration. Minimum is 1 minute.", ephemeral=True)
            return
        # Pre-fetch the proposal so the poll title/embed can include details
        # before round-tripping through the local DB.
        proposal = await _interspace_get(f"/api/proposals/{proposal_id}")
        if proposal is None:
            await ctx.respond(
                f"Could not find proposal **{proposal_id}** on Interspace.",
                ephemeral=True,
            )
            return
        if proposal.get("status") not in ("approved", "creator_claimed", "submitted"):
            await ctx.respond(
                f"Proposal **{proposal_id}** is not approved (status: {proposal.get('status')}).",
                ephemeral=True,
            )
            return

        title = proposal.get("title") or proposal_id

        # Local bot DB (still useful for /poll status and timer-driven closure)
        poll_id = await db.create_stage_poll(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            title=title,
            options=opts,
            role_ids=[],  # role gating now lives on Interspace, derived from tier
            num_tiers=num_tiers,
            duration_seconds=dur_sec,
            preference_duration_seconds=pref_dur_sec,
        )

        # Register poll on Interspace so the web UI knows about it.
        import datetime
        try:
            closes_at_iso = (datetime.datetime.utcnow() + datetime.timedelta(seconds=dur_sec)).isoformat() + "Z"
        except Exception:
            closes_at_iso = None

        interspace_result = await _interspace_post("/api/polls/open", {
            "botPollId": poll_id,
            "guildId": ctx.guild.id,
            "channelId": ctx.channel.id,
            "proposalId": proposal_id,
            "options": opts,
            "numTiers": num_tiers,
            "closesAt": closes_at_iso,
            "preferenceDurationSeconds": pref_dur_sec,
        })

        interspace_note = ""
        if INTERSPACE_URL:
            if interspace_result:
                interspace_note = f"\n\n🌐 **Vote on Interspace:** {INTERSPACE_URL}"
            else:
                interspace_note = "\n\n⚠️ Interspace registration failed — voting may not be available on web."

        # Compose tier hint for the announcement
        tier = proposal.get("tier")
        sub = proposal.get("tier3SubType")
        if tier == 3 and sub:
            tier_label = f"Tier 3 — Type {sub}"
            who = "Overseers only" if sub == "I" else "Overseers + Admins"
        elif tier == 2:
            tier_label = "Tier 2"
            who = "Format Council"
        elif tier == 1:
            tier_label = "Tier 1"
            who = "Everyone"
        else:
            tier_label = "Untiered"
            who = "(see Interspace)"

        opt_lines = "\n".join([f"`{i+1}`. {opt}" for i, opt in enumerate(opts)])
        embed = discord.Embed(
            title=f"⚖️ Stage 1 Open: {title}",
            description=(
                f"**Poll ID:** `{poll_id}`\n"
                f"**Proposal:** `{proposal_id}` ({tier_label} • {who})\n"
                f"**Tier range:** 1 to {num_tiers} (1 = most balanced)\n\n"
                f"**Options:**\n{opt_lines}"
                f"{interspace_note}"
            ),
            color=0x345995,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(
            text=(
                f"Stage 1 closes in {format_duration(dur_sec)} • "
                f"Voting is done via the Interspace web UI"
            )
        )
        await ctx.channel.send(embed=embed)

        # Also drop a copy into the petition hall in the requested format.
        await post_to_petition_hall(ctx.guild, proposal)

        await ctx.respond(f"Two-stage poll created with ID `{poll_id}` for proposal `{proposal_id}`.", ephemeral=True)

    @poll_group.command(name="stage_close", description="Close Stage 1 and fetch/post results from Interspace.")
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
        await self._close_stage1_and_post(ctx.guild, poll, ctx.channel)
        await self._refresh_live_status_messages(ctx.guild.id, "stage", poll_id)
        await ctx.respond("Stage 1 closed and report posted.", ephemeral=True)

    async def _close_stage1_and_post(
        self,
        guild: discord.Guild,
        poll: dict,
        channel: discord.abc.Messageable,
    ) -> None:
        poll_id = int(poll["id"])
        options: list[str] = json.loads(poll["options"])

        # Ask Interspace to compute the Stage 1 result from locked Interspace votes
        interspace_id = f"stage-{poll_id}"
        result = await _interspace_post_compute(f"/api/polls/{interspace_id}/compute")

        if result is None:
            # Interspace unreachable — mark poll failed locally and post an error
            await db.set_stage_poll_status(poll_id, "failed_stage1")
            embed = discord.Embed(
                title=f"📊 Stage 1 Results: {poll['title']} (ID {poll_id})",
                description="❌ Could not reach Interspace to compute results. Poll marked as failed.",
                color=0x8B0000,
                timestamp=discord.utils.utcnow(),
            )
            await channel.send(embed=embed)
            return

        # Build Discord embed from Interspace result
        report_lines_raw = result.get("reportLines", [])
        report_lines: list[str] = []
        for r in report_lines_raw:
            opt = r.get("option", "?")
            res = r.get("result", "")
            tier = r.get("assignedTier")
            if tier:
                if res == "simpson":
                    report_lines.append(
                        f"**{opt}** → Tier {tier} via Simpson "
                        f"(mode {r.get('modeFreqPct')}% ≥ {r.get('pWinPct')}%, n_eff={r.get('nEff')})"
                    )
                else:
                    report_lines.append(
                        f"**{opt}** → Tier {tier} via EV fallback "
                        f"(E={r.get('ev')}, CI90=({r.get('ciLow')}, {r.get('ciHigh')}))"
                    )
            else:
                report_lines.append(f"**{opt}** → inconclusive ({r.get('reason', res)})")

        outcome = result.get("outcome", "")
        needs_stage2 = result.get("needsStage2", False)
        new_status = result.get("status", "failed_stage1")

        await db.set_stage_poll_status(poll_id, new_status)

        report_text = "\n".join(report_lines)
        embed = discord.Embed(
            title=f"📊 Stage 1 Results: {poll['title']} (ID {poll_id})",
            description=f"{report_text}\n\n{outcome}",
            color=0x2E86AB if "passed" in new_status or "preference" in new_status else 0x8B0000,
            timestamp=discord.utils.utcnow(),
        )
        if needs_stage2 and INTERSPACE_URL:
            embed.add_field(
                name="Stage 2 Voting",
                value=f"🌐 Cast your Stage 2 preference at: {INTERSPACE_URL}",
                inline=False,
            )
        await channel.send(embed=embed)

        if needs_stage2:
            pref_closes_at = result.get("preferenceClosesAt")
            await db.set_stage_poll_status(poll_id, "preference_open",
                                           preference_options=result.get("stage2OptionIndices", []))

    async def _close_preference_and_post(
        self,
        guild: discord.Guild,
        stage_poll: dict,
        channel: discord.abc.Messageable,
    ) -> None:
        stage_id = int(stage_poll["id"])
        interspace_id = f"stage-{stage_id}"
        result = await _interspace_post_compute(f"/api/polls/{interspace_id}/compute-preference")

        if result is None:
            await db.set_stage_poll_status(stage_id, "failed_preference")
            embed = discord.Embed(
                title=f"📊 Stage 2 Results: {stage_poll['title']} (ID {stage_id})",
                description="❌ Could not reach Interspace to compute Stage 2 results.",
                color=0x8B0000,
                timestamp=discord.utils.utcnow(),
            )
            await channel.send(embed=embed)
            return

        outcome = result.get("outcome", "")
        new_status = result.get("status", "failed_preference")
        await db.set_stage_poll_status(stage_id, new_status)

        counts_data = result.get("counts", [])
        lines = [f"**{item['option']}** — {item['count']} vote(s)" for item in counts_data]

        embed = discord.Embed(
            title=f"📊 Stage 2 Results: {stage_poll['title']} (ID {stage_id})",
            description="\n".join(lines + ["", outcome]),
            color=0x2E86AB if "passed" in new_status else 0x8B0000,
            timestamp=discord.utils.utcnow(),
        )
        await channel.send(embed=embed)

    @poll_group.command(name="status", description="View current results/status for a poll ID.")
    async def poll_status(
        self,
        ctx: discord.ApplicationContext,
        poll_id: Option(int, "Poll ID", required=True),
        live: Option(bool, "Post/update a persistent in-channel status message", required=False, default=False),
    ) -> None:
        if not ctx.guild:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return

        poll = await db.get_poll_by_id(poll_id)
        if poll and poll["guild_id"] == ctx.guild.id:
            embed = await self._build_regular_status_embed(ctx.guild, poll)
            if live:
                if not isinstance(ctx.channel, discord.TextChannel):
                    await ctx.respond("Use live status in a server text channel.", ephemeral=True)
                    return
                displays = await db.get_live_poll_statuses(ctx.guild.id, "regular", poll_id)
                existing = next((d for d in displays if d[1] == ctx.channel.id), None)
                if existing:
                    row_id, _, msg_id = existing
                    try:
                        msg = await ctx.channel.fetch_message(msg_id)
                        await msg.edit(embed=embed)
                    except discord.NotFound:
                        await db.delete_live_poll_status_row(row_id)
                        msg = await ctx.channel.send(embed=embed)
                        await db.upsert_live_poll_status(ctx.guild.id, "regular", poll_id, ctx.channel.id, msg.id)
                else:
                    msg = await ctx.channel.send(embed=embed)
                    await db.upsert_live_poll_status(ctx.guild.id, "regular", poll_id, ctx.channel.id, msg.id)
                await ctx.respond("Live poll status posted.", ephemeral=True)
            else:
                await ctx.respond(embed=embed, ephemeral=True)
            return

        stage = await db.get_stage_poll_by_id(poll_id)
        if stage and stage["guild_id"] == ctx.guild.id:
            embed = await self._build_stage_status_embed(stage)
            if live:
                if not isinstance(ctx.channel, discord.TextChannel):
                    await ctx.respond("Use live status in a server text channel.", ephemeral=True)
                    return
                displays = await db.get_live_poll_statuses(ctx.guild.id, "stage", poll_id)
                existing = next((d for d in displays if d[1] == ctx.channel.id), None)
                if existing:
                    row_id, _, msg_id = existing
                    try:
                        msg = await ctx.channel.fetch_message(msg_id)
                        await msg.edit(embed=embed)
                    except discord.NotFound:
                        await db.delete_live_poll_status_row(row_id)
                        msg = await ctx.channel.send(embed=embed)
                        await db.upsert_live_poll_status(ctx.guild.id, "stage", poll_id, ctx.channel.id, msg.id)
                else:
                    msg = await ctx.channel.send(embed=embed)
                    await db.upsert_live_poll_status(ctx.guild.id, "stage", poll_id, ctx.channel.id, msg.id)
                await ctx.respond("Live stage poll status posted.", ephemeral=True)
            else:
                await ctx.respond(embed=embed, ephemeral=True)
            return

        await ctx.respond(f"No poll with ID **{poll_id}** in this server.", ephemeral=True)

    @poll_group.command(name="delete", description="Remove a poll (Mod/Admin).")
    async def poll_delete(
        self,
        ctx: discord.ApplicationContext,
        poll_id: Option(int, "Poll ID", required=True),
        delete_message: Option(bool, "Try to delete the poll message (default: true)", required=False) = True,
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
        await self._refresh_live_status_messages(ctx.guild.id, "regular", poll_id)
        await ctx.respond(f"Poll **{poll_id}** removed.", ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.user_id == self.bot.user.id:
            return
        await self._handle_reaction(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload, add=False)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent, *, add: bool) -> None:
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
        if payload.guild_id:
            await self._refresh_live_status_messages(payload.guild_id, "regular", int(poll["id"]))

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
            f"**1. Active Eligible Voters:** {num_active_eligible}",
            f"**2. Valid Voters:** {num_valid_voters}",
            f"**3. Valid Votes:** {total_valid_votes}",
            f"**4. Eligible Participation:** {pct_eligible_voted:.1f}%",
            f"**5. Simpson Threshold:** {pwin_required * 100:.1f}% (n_eff={n_eff:.3f})",
            f"**6. Winning Percentage:** {winning_pct:.1f}% ({winning_label})",
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

    @tasks.loop(seconds=30)
    async def check_poll_closures(self) -> None:
        pending = await db.get_pending_polls()
        for poll in pending:
            if str(poll.get("title", "")).startswith("[Stage 2] "):
                continue
            await self._close_poll(poll)
            await self._refresh_live_status_messages(int(poll["guild_id"]), "regular", int(poll["id"]))
            await db.delete_poll(poll["id"])

        pending_stage1 = await db.get_pending_stage1_polls()
        for poll in pending_stage1:
            guild = self.bot.get_guild(poll["guild_id"])
            if not guild:
                continue
            channel = guild.get_channel(poll["channel_id"])
            if not channel:
                try:
                    channel = await guild.fetch_channel(poll["channel_id"])
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
            if not channel or not isinstance(channel, discord.TextChannel):
                continue
            await self._close_stage1_and_post(guild, poll, channel)
            await self._refresh_live_status_messages(int(poll["guild_id"]), "stage", int(poll["id"]))

        pending_pref = await db.get_pending_preference_polls()
        for poll in pending_pref:
            guild = self.bot.get_guild(poll["guild_id"])
            if not guild:
                continue
            channel = guild.get_channel(poll["channel_id"])
            if not channel:
                try:
                    channel = await guild.fetch_channel(poll["channel_id"])
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
            if not channel or not isinstance(channel, discord.TextChannel):
                continue
            await self._close_preference_and_post(guild, poll, channel)
            await self._refresh_live_status_messages(int(poll["guild_id"]), "stage", int(poll["id"]))

        for guild_id, kind, poll_id in await db.list_live_poll_status_targets():
            await self._refresh_live_status_messages(guild_id, kind, poll_id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.check_poll_closures.is_running():
            self.check_poll_closures.start()
