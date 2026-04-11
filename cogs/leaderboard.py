"""Leaderboard cog - ELO ranking for members + archetype tier list."""

from typing import Any

import discord
from discord import Option
from discord.ext import commands

import database as db
from database import AaEloSettings, EloSettings, format_elo


def _parse_hex_color(s: str | None) -> int | None:
    if not s or not str(s).strip():
        return None
    t = str(s).strip().removeprefix("#")
    if len(t) != 6:
        return None
    try:
        return int(t, 16)
    except ValueError:
        return None


async def _patch_leaderboard_settings(
    guild_id: int,
    leaderboard: str,
    updates: dict[str, Any],
    *,
    undo_ctx: discord.ApplicationContext | None = None,
) -> tuple[int, EloSettings] | None:
    lb_id = await db.get_leaderboard_id(guild_id, _parse_leaderboard_name_from_autocomplete(leaderboard))
    if not lb_id:
        return None
    if undo_ctx and undo_ctx.guild and undo_ctx.author:
        prev = await db.get_leaderboard_elo_settings_raw(lb_id)
        if prev is not None:
            await db.push_undo(
                guild_id,
                undo_ctx.author.id,
                {"kind": "settings", "leaderboard_id": lb_id, "prev_json": prev},
            )
    s = await db.get_leaderboard_settings(lb_id)
    d = s.to_dict()
    d.update(updates)
    merged = EloSettings.from_dict(d)
    await db.set_leaderboard_settings(lb_id, merged)
    return (lb_id, merged)


async def leaderboard_autocomplete(ctx: discord.AutocompleteContext) -> list[str]:
    if not ctx.interaction.guild_id:
        return []
    boards = await db.list_leaderboards(ctx.interaction.guild_id)
    out = []
    for _lid, name, fmt in boards:
        out.append(f"{name} [2v2]" if fmt == "2v2" else name)
    return out


def _parse_leaderboard_name_from_autocomplete(leaderboard: str) -> str:
    """Strip optional [2v2] suffix from autocomplete display."""
    s = leaderboard.strip()
    if s.endswith("[2v2]"):
        return s[: -len("[2v2]")].rstrip()
    return s


def _mod_perms(author: discord.Member | discord.User | None) -> bool:
    if not author or not isinstance(author, discord.Member):
        return False
    p = author.guild_permissions
    return bool(p.administrator or p.manage_guild or p.manage_messages)


class LeaderboardCog(commands.Cog):
    """ELO leaderboard (members) and archetype tier list."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _build_rankings_embed(self, leaderboard_id: int) -> discord.Embed | None:
        meta = await db.get_leaderboard_by_id(leaderboard_id)
        if not meta:
            return None
        entries = await db.get_member_leaderboard(leaderboard_id)
        settings = await db.get_leaderboard_settings(leaderboard_id)
        lb_name = meta["name"]
        title = settings.display_title or lb_name
        header = settings.description or ""
        if not entries:
            body = "*No players on this leaderboard yet.*"
        else:
            lines = []
            for i, (uid, disp, elo) in enumerate(entries, 1):
                medal = {"1": "🥇", "2": "🥈", "3": "🥉"}.get(str(i), f"`{i}.`")
                name = disp or f"<@{uid}>"
                lines.append(f"{medal} **{name}** — {format_elo(elo, settings.precision)} ELO")
            body = "\n".join(lines)
        desc = f"{header}\n\n{body}" if header.strip() else body
        color = settings.primary_color if settings.primary_color is not None else 0xFFD700
        embed = discord.Embed(
            title=f"📊 {title}",
            description=desc,
            color=color,
        )
        if settings.icon_url:
            embed.set_thumbnail(url=settings.icon_url)
        if settings.banner_url:
            embed.set_image(url=settings.banner_url)
        embed.set_footer(text="Live • Updates when matches, roster, or settings change")
        return embed

    async def _build_tierlist_embed(self, guild_id: int) -> discord.Embed:
        entries = await db.get_tier_list(guild_id)
        aa = await db.get_aa_settings(guild_id)
        if not entries:
            desc = (
                f"*No archetypes meet the public tier list minimum yet "
                f"({aa.min_games_display} games with AA-ELO tracking). "
                f"Record **1v1** matches to build data.*"
            )
        else:
            lines = []
            for i, (_, display, elo) in enumerate(entries, 1):
                medal = {"1": "🥇", "2": "🥈", "3": "🥉"}.get(str(i), f"`{i}.`")
                lines.append(f"{medal} **{display}** — {int(elo)} ELO")
            desc = "\n".join(lines)
        embed = discord.Embed(
            title="📈 Meta Tier List (Archetype ELO)",
            description=desc,
            color=0x9370DB,
        )
        embed.set_footer(
            text=f"AA-ELO (1v1) • min. {aa.min_games_display} games to appear • Updates on 1v1 matches / reset"
        )
        return embed

    async def refresh_rankings_displays(self, guild_id: int, leaderboard_id: int) -> None:
        displays = await db.get_live_displays(guild_id, leaderboard_id, "rankings")
        if not displays:
            return
        embed = await self._build_rankings_embed(leaderboard_id)
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        if embed is None:
            for row_id, _, _ in displays:
                await db.delete_live_display_row(row_id)
            return
        for row_id, channel_id, message_id in displays:
            channel = guild.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                await db.delete_live_display_row(row_id)
                continue
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(embed=embed)
            except discord.NotFound:
                await db.delete_live_display_row(row_id)
            except discord.HTTPException:
                pass

    async def refresh_tierlist_displays(self, guild_id: int) -> None:
        sentinel = db.LIVE_TIERLIST_LEADERBOARD_ID
        displays = await db.get_live_displays(guild_id, sentinel, "tierlist")
        if not displays:
            return
        embed = await self._build_tierlist_embed(guild_id)
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        for row_id, channel_id, message_id in displays:
            channel = guild.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                await db.delete_live_display_row(row_id)
                continue
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(embed=embed)
            except discord.NotFound:
                await db.delete_live_display_row(row_id)
            except discord.HTTPException:
                pass

    async def _respond_rankings(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: str | None,
    ) -> None:
        guild = ctx.guild
        if not guild or not isinstance(ctx.channel, discord.TextChannel):
            await ctx.respond("Use this command in a server text channel.", ephemeral=True)
            return
        guild_id = guild.id
        boards = await db.list_leaderboards(guild_id)
        if not boards:
            await ctx.respond("No leaderboards yet.")
            return
        if leaderboard:
            lb_id = await db.get_leaderboard_id(guild_id, _parse_leaderboard_name_from_autocomplete(leaderboard))
            if not lb_id:
                await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
                return
        else:
            lb_id = boards[0][0]

        embed = await self._build_rankings_embed(lb_id)
        if not embed:
            await ctx.respond("Leaderboard not found.", ephemeral=True)
            return

        channel_id = ctx.channel.id
        displays = await db.get_live_displays(guild_id, lb_id, "rankings")
        existing = next((d for d in displays if d[1] == channel_id), None)

        try:
            if existing:
                row_id, _, msg_id = existing
                try:
                    msg = await ctx.channel.fetch_message(msg_id)
                    await msg.edit(embed=embed)
                    await ctx.respond(
                        "Leaderboard message updated in this channel. It will keep refreshing automatically.",
                        ephemeral=True,
                    )
                except discord.NotFound:
                    await db.delete_live_display_row(row_id)
                    msg = await ctx.channel.send(embed=embed)
                    await db.upsert_live_display(guild_id, lb_id, "rankings", channel_id, msg.id)
                    await ctx.respond(
                        "Posted a new live leaderboard (the old message was deleted).",
                        ephemeral=True,
                    )
            else:
                msg = await ctx.channel.send(embed=embed)
                await db.upsert_live_display(guild_id, lb_id, "rankings", channel_id, msg.id)
                await ctx.respond(
                    "Posted a **live** leaderboard in this channel. It updates automatically when rankings change.",
                    ephemeral=True,
                )
        except discord.HTTPException as e:
            await ctx.respond(f"Could not post or edit the leaderboard message: {e}", ephemeral=True)

    async def _respond_tierlist(self, ctx: discord.ApplicationContext) -> None:
        guild = ctx.guild
        if not guild or not isinstance(ctx.channel, discord.TextChannel):
            await ctx.respond("Use this command in a server text channel.", ephemeral=True)
            return
        guild_id = guild.id
        sentinel = db.LIVE_TIERLIST_LEADERBOARD_ID
        embed = await self._build_tierlist_embed(guild_id)
        channel_id = ctx.channel.id
        displays = await db.get_live_displays(guild_id, sentinel, "tierlist")
        existing = next((d for d in displays if d[1] == channel_id), None)

        try:
            if existing:
                row_id, _, msg_id = existing
                try:
                    msg = await ctx.channel.fetch_message(msg_id)
                    await msg.edit(embed=embed)
                    await ctx.respond(
                        "Tier list message updated in this channel. It will keep refreshing automatically.",
                        ephemeral=True,
                    )
                except discord.NotFound:
                    await db.delete_live_display_row(row_id)
                    msg = await ctx.channel.send(embed=embed)
                    await db.upsert_live_display(guild_id, sentinel, "tierlist", channel_id, msg.id)
                    await ctx.respond(
                        "Posted a new live tier list (the old message was deleted).",
                        ephemeral=True,
                    )
            else:
                msg = await ctx.channel.send(embed=embed)
                await db.upsert_live_display(guild_id, sentinel, "tierlist", channel_id, msg.id)
                await ctx.respond(
                    "Posted a **live** tier list in this channel. It updates automatically when decks gain/lose ELO.",
                    ephemeral=True,
                )
        except discord.HTTPException as e:
            await ctx.respond(f"Could not post or edit the tier list message: {e}", ephemeral=True)

    leaderboard_group = discord.SlashCommandGroup("leaderboard", "ELO leaderboard for members")

    settings_group = leaderboard_group.create_subgroup("settings", "Customize ELO settings")
    aa_group = leaderboard_group.create_subgroup("aa", "Adjusted Archetype ELO (1v1 tier list)")

    # --- AA-ELO (guild-wide) ---

    @aa_group.command(name="view", description="View AA-ELO parameters for the archetype tier list")
    async def aa_view(self, ctx: discord.ApplicationContext) -> None:
        if not ctx.guild:
            return
        aa = await db.get_aa_settings(ctx.guild.id)
        await ctx.respond(
            ephemeral=True,
            content=(
                f"**K_arch** {aa.k_arch} — base archetype K (max swing per game at high sample)\n"
                f"**n₀** {aa.n0} — sparsity prior (K_eff half of K_arch when pair count n = n₀)\n"
                f"**influence_range** {aa.influence_range} — logistic denominator for E_player / E_archetype\n"
                f"**min_games_display** {aa.min_games_display} — min tracked games per archetype to show on tier list"
            ),
        )

    @aa_group.command(name="k_arch", description="Set base K for archetype AA-ELO updates")
    async def aa_k_arch(
        self,
        ctx: discord.ApplicationContext,
        value: Option(float, "Recommended ~20", required=True),
    ) -> None:
        if not ctx.guild or not ctx.author:
            return
        if value < 1 or value > 200:
            await ctx.respond("Use a value between 1 and 200.", ephemeral=True)
            return
        prev = await db.get_aa_settings(ctx.guild.id)
        new = AaEloSettings(
            k_arch=value,
            n0=prev.n0,
            influence_range=prev.influence_range,
            min_games_display=prev.min_games_display,
        )
        await db.set_aa_settings(ctx.guild.id, new)
        await db.push_undo(ctx.guild.id, ctx.author.id, {"kind": "aa_settings", "prev": prev.to_dict()})
        await ctx.respond(f"**K_arch** set to **{value}**.", ephemeral=True)
        await self.refresh_tierlist_displays(ctx.guild.id)

    @aa_group.command(name="n0", description="Set sparsity prior n₀ for archetype pair dampening")
    async def aa_n0(
        self,
        ctx: discord.ApplicationContext,
        value: Option(float, "Recommended ~10", required=True),
    ) -> None:
        if not ctx.guild or not ctx.author:
            return
        if value < 0.5 or value > 500:
            await ctx.respond("Use a value between 0.5 and 500.", ephemeral=True)
            return
        prev = await db.get_aa_settings(ctx.guild.id)
        new = AaEloSettings(
            k_arch=prev.k_arch,
            n0=value,
            influence_range=prev.influence_range,
            min_games_display=prev.min_games_display,
        )
        await db.set_aa_settings(ctx.guild.id, new)
        await db.push_undo(ctx.guild.id, ctx.author.id, {"kind": "aa_settings", "prev": prev.to_dict()})
        await ctx.respond(f"**n₀** set to **{value}**.", ephemeral=True)

    @aa_group.command(name="influence", description="Logistic denominator for AA-ELO expectations (often 400)")
    async def aa_influence(
        self,
        ctx: discord.ApplicationContext,
        value: Option(float, "Usually 400", required=True),
    ) -> None:
        if not ctx.guild or not ctx.author:
            return
        if value < 50 or value > 2000:
            await ctx.respond("Use a value between 50 and 2000.", ephemeral=True)
            return
        prev = await db.get_aa_settings(ctx.guild.id)
        new = AaEloSettings(
            k_arch=prev.k_arch,
            n0=prev.n0,
            influence_range=value,
            min_games_display=prev.min_games_display,
        )
        await db.set_aa_settings(ctx.guild.id, new)
        await db.push_undo(ctx.guild.id, ctx.author.id, {"kind": "aa_settings", "prev": prev.to_dict()})
        await ctx.respond(f"**influence_range** set to **{value}**.", ephemeral=True)

    @aa_group.command(name="min_games", description="Min archetype game count to appear on public tier list")
    async def aa_min_games(
        self,
        ctx: discord.ApplicationContext,
        value: Option(int, "Recommended ~5", required=True),
    ) -> None:
        if not ctx.guild or not ctx.author:
            return
        if value < 0 or value > 10000:
            await ctx.respond("Use a value between 0 and 10000.", ephemeral=True)
            return
        prev = await db.get_aa_settings(ctx.guild.id)
        new = AaEloSettings(
            k_arch=prev.k_arch,
            n0=prev.n0,
            influence_range=prev.influence_range,
            min_games_display=value,
        )
        await db.set_aa_settings(ctx.guild.id, new)
        await db.push_undo(ctx.guild.id, ctx.author.id, {"kind": "aa_settings", "prev": prev.to_dict()})
        await ctx.respond(f"**min_games_display** set to **{value}**.", ephemeral=True)
        await self.refresh_tierlist_displays(ctx.guild.id)

    # --- ELO Settings ---

    @settings_group.command(name="view", description="View current ELO settings for a leaderboard")
    async def settings_view(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        lb_id = await db.get_leaderboard_id(guild_id, _parse_leaderboard_name_from_autocomplete(leaderboard))
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        s = await db.get_leaderboard_settings(lb_id)
        ma = "∞ (no cap)" if s.max_advantage <= 0 else str(s.max_advantage)
        cap_r = s.influence_range if s.cap_range <= 0 else s.cap_range
        embed = discord.Embed(
            title=f"⚙️ ELO Settings — {leaderboard}",
            color=0x1E90FF,
        )
        embed.add_field(
            name="Core",
            value=(
                f"**Default Rating:** {s.default_rating}\n"
                f"**K Factor:** {s.k_factor}\n"
                f"**Precision:** {s.precision} decimal(s)\n"
                f"**Loss Dampen:** {s.loss_dampen} (1.0 = full loss)\n"
                f"**Matches Required:** {s.matches_required_for_ranking}\n"
                f"**Inactive Hide (days):** {s.inactive_days_threshold}\n"
                f"**Locked:** {s.locked}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Rating curve",
            value=(
                f"**Max Advantage / match:** {ma}\n"
                f"**Curve base:** {s.curve_factor} (usually 10)\n"
                f"**Influence range:** {s.influence_range}\n"
                f"**FFA distribution:** {s.ffa_distribution}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Gap capping",
            value=(
                f"**Cap range:** {cap_r}\n"
                f"**Favorite win / underdog loss:** {s.cap_favorite_win_impact} / {s.cap_underdog_loss_impact}\n"
                f"**Underdog win / favorite loss:** {s.cap_underdog_win_impact} / {s.cap_favorite_loss_impact}"
            ),
            inline=False,
        )
        disp = (
            f"**Title:** {s.display_title or '*(leaderboard name)*'}\n"
            f"**Description:** {(s.description or '')[:180] or '*(none)*'}\n"
            f"**Icon URL:** {s.icon_url or '*(none)*'}\n"
            f"**Banner URL:** {s.banner_url or '*(none)*'}"
        )
        pc = f"#{s.primary_color:06x}" if s.primary_color is not None else "*(default)*"
        sc = f"#{s.secondary_color:06x}" if s.secondary_color is not None else "*(default)*"
        embed.add_field(name="Display", value=disp + f"\n**Primary:** {pc} **Secondary:** {sc}", inline=False)
        meta = await db.get_leaderboard_by_id(lb_id)
        fmt = (meta or {}).get("match_format", "1v1")
        aa = await db.get_aa_settings(guild_id)
        embed.add_field(
            name="Match format",
            value=f"`{fmt}`" + (" — use `/leaderboard match_2v2` for games." if fmt == "2v2" else ""),
            inline=False,
        )
        embed.add_field(
            name="AA-ELO (guild · 1v1 archetypes)",
            value=(
                f"**K_arch** {aa.k_arch} · **n₀** {aa.n0} · **influence** {aa.influence_range}\n"
                f"**Min games (tier list)** {aa.min_games_display} (`/leaderboard aa`)"
            ),
            inline=False,
        )
        await ctx.respond(embed=embed)

    async def _refresh_rankings_after_settings(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: str,
    ) -> None:
        if not ctx.guild:
            return
        lid = await db.get_leaderboard_id(ctx.guild.id, _parse_leaderboard_name_from_autocomplete(leaderboard))
        if lid:
            await self.refresh_rankings_displays(ctx.guild.id, lid)

    async def _set_setting(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: str,
        field: str,
        value: float | int,
        min_val: float | None = None,
        max_val: float | None = None,
    ) -> bool:
        guild_id = ctx.guild.id if ctx.guild else 0
        lb_id = await db.get_leaderboard_id(guild_id, _parse_leaderboard_name_from_autocomplete(leaderboard))
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return False
        if min_val is not None and value < min_val:
            await ctx.respond(f"{field} must be at least {min_val}.", ephemeral=True)
            return False
        if max_val is not None and value > max_val:
            await ctx.respond(f"{field} must be at most {max_val}.", ephemeral=True)
            return False
        s = await db.get_leaderboard_settings(lb_id)
        d = s.to_dict()
        key = field.lower().replace(" ", "_")
        if key not in d:
            await ctx.respond(f"Unknown setting: {field}.", ephemeral=True)
            return False
        if key == "precision":
            d[key] = int(value)
        elif key in ("matches_required_for_ranking", "inactive_days_threshold"):
            d[key] = int(value)
        else:
            d[key] = float(value)
        s = EloSettings.from_dict(d)
        if ctx.guild and ctx.author:
            prev = await db.get_leaderboard_elo_settings_raw(lb_id)
            if prev is not None:
                await db.push_undo(
                    guild_id,
                    ctx.author.id,
                    {"kind": "settings", "leaderboard_id": lb_id, "prev_json": prev},
                )
        await db.set_leaderboard_settings(lb_id, s)
        return True

    @settings_group.command(name="default_rating", description="Set default starting ELO")
    async def set_default_rating(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(float, "Default rating (e.g. 1000)", required=True),
    ) -> None:
        if await self._set_setting(ctx, leaderboard, "default_rating", value, 0, 10000):
            await ctx.respond(f"Default rating set to **{value}** for **{leaderboard}**.")
            await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="k_factor", description="Set K factor (ELO sensitivity)")
    async def set_k_factor(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(float, "K factor (e.g. 32)", required=True),
    ) -> None:
        if await self._set_setting(ctx, leaderboard, "k_factor", value, 1, 100):
            await ctx.respond(f"K factor set to **{value}** for **{leaderboard}**.")
            await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="precision", description="Decimal places for ELO display")
    async def set_precision(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(int, "Precision (0 = whole numbers)", required=True),
    ) -> None:
        if await self._set_setting(ctx, leaderboard, "precision", value, 0, 4):
            await ctx.respond(f"Precision set to **{value}** for **{leaderboard}**.")
            await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="loss_dampen", description="Reduce ELO loss for loser (e.g. 0.5 = half loss)")
    async def set_loss_dampen(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(float, "Loss dampen (0.0 to 1.0)", required=True),
    ) -> None:
        if await self._set_setting(ctx, leaderboard, "loss_dampen", value, 0, 1):
            await ctx.respond(f"Loss dampen set to **{value}** for **{leaderboard}**.")
            await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="max_advantage", description="Cap max ELO gain/loss per game (0 = unlimited)")
    async def set_max_advantage(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(float, "Max advantage (0 = no cap)", required=True),
    ) -> None:
        if await self._set_setting(ctx, leaderboard, "max_advantage", value, 0, 100000):
            await ctx.respond(f"Max advantage set to **{value}** for **{leaderboard}**.")
            await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="curve_factor", description="Exponent base in ELO formula (standard = 10)")
    async def set_curve_factor(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(float, "Curve base (usually 10)", required=True),
    ) -> None:
        if await self._set_setting(ctx, leaderboard, "curve_factor", value, 2, 50):
            await ctx.respond(f"Curve factor set to **{value}** for **{leaderboard}**.")
            await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="influence_range", description="Denominator in ELO logistic (often 400)")
    async def set_influence_range(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(float, "Influence range", required=True),
    ) -> None:
        if await self._set_setting(ctx, leaderboard, "influence_range", value, 1, 5000):
            await ctx.respond(f"Influence range set to **{value}** for **{leaderboard}**.")
            await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="ffa_distribution", description="FFA distribution")
    async def set_ffa_distribution(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(float, "FFA distribution", required=True),
    ) -> None:
        if await self._set_setting(ctx, leaderboard, "ffa_distribution", value, 0, 2):
            await ctx.respond(f"FFA distribution set to **{value}** for **{leaderboard}**.")
            await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="matches_required", description="Minimum matches before a player appears on the ranked board")
    async def settings_matches_required(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(int, "Minimum matches (0 = show everyone)", required=True),
    ) -> None:
        if not ctx.guild:
            return
        if value < 0 or value > 100000:
            await ctx.respond("Value must be between 0 and 100000.", ephemeral=True)
            return
        r = await _patch_leaderboard_settings(
            ctx.guild.id, leaderboard, {"matches_required_for_ranking": value}, undo_ctx=ctx
        )
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"Matches required set to **{value}** for **{leaderboard}**.")
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="inactive_days", description="Hide players after this many days without a match (0 = never)")
    async def settings_inactive_days(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(int, "Days of inactivity before hiding", required=True),
    ) -> None:
        if not ctx.guild:
            return
        if value < 0 or value > 3650:
            await ctx.respond("Value must be between 0 and 3650.", ephemeral=True)
            return
        r = await _patch_leaderboard_settings(
            ctx.guild.id, leaderboard, {"inactive_days_threshold": value}, undo_ctx=ctx
        )
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"Inactive threshold set to **{value}** day(s) for **{leaderboard}**.")
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="lock", description="Lock leaderboard (no new match results)")
    async def settings_lock(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        locked: Option(bool, "True = locked", required=True),
    ) -> None:
        if not ctx.guild:
            return
        r = await _patch_leaderboard_settings(ctx.guild.id, leaderboard, {"locked": locked}, undo_ctx=ctx)
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"Leaderboard **{leaderboard}** is now **{'locked' if locked else 'unlocked'}**.")
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="cap_range", description="Max rating gap used when capping (0 = use influence range)")
    async def settings_cap_range(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(float, "Cap range (0 = same as influence range)", required=True),
    ) -> None:
        if not ctx.guild:
            return
        if value < 0 or value > 100000:
            await ctx.respond("Value must be between 0 and 100000.", ephemeral=True)
            return
        r = await _patch_leaderboard_settings(ctx.guild.id, leaderboard, {"cap_range": value}, undo_ctx=ctx)
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"Cap range set to **{value}** for **{leaderboard}**.")
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="cap_favorite_win", description="Apply gap cap when the favorite wins")
    async def settings_cap_favorite_win(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(bool, "Enable capping for this case", required=True),
    ) -> None:
        if not ctx.guild:
            return
        r = await _patch_leaderboard_settings(
            ctx.guild.id, leaderboard, {"cap_favorite_win_impact": value}, undo_ctx=ctx
        )
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"`cap_favorite_win` = **{value}** for **{leaderboard}**.")
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="cap_favorite_loss", description="Pair with underdog win (upset) — gap cap flags")
    async def settings_cap_favorite_loss(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(bool, "Enable capping for this case", required=True),
    ) -> None:
        if not ctx.guild:
            return
        r = await _patch_leaderboard_settings(
            ctx.guild.id, leaderboard, {"cap_favorite_loss_impact": value}, undo_ctx=ctx
        )
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"`cap_favorite_loss` = **{value}** for **{leaderboard}**.")
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="cap_underdog_win", description="Apply gap cap when the underdog wins (upset)")
    async def settings_cap_underdog_win(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(bool, "Enable capping for this case", required=True),
    ) -> None:
        if not ctx.guild:
            return
        r = await _patch_leaderboard_settings(
            ctx.guild.id, leaderboard, {"cap_underdog_win_impact": value}, undo_ctx=ctx
        )
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"`cap_underdog_win` = **{value}** for **{leaderboard}**.")
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="cap_underdog_loss", description="Pair with favorite win — gap cap flags")
    async def settings_cap_underdog_loss(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(bool, "Enable capping for this case", required=True),
    ) -> None:
        if not ctx.guild:
            return
        r = await _patch_leaderboard_settings(
            ctx.guild.id, leaderboard, {"cap_underdog_loss_impact": value}, undo_ctx=ctx
        )
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"`cap_underdog_loss` = **{value}** for **{leaderboard}**.")
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="display_title", description="Custom title on the leaderboard embed (empty to clear)")
    async def settings_display_title(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(str, "Title text (leave empty to use leaderboard name)", required=False, default=None),
    ) -> None:
        if not ctx.guild:
            return
        title = value.strip() if value and value.strip() else None
        r = await _patch_leaderboard_settings(ctx.guild.id, leaderboard, {"display_title": title}, undo_ctx=ctx)
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"Display title updated for **{leaderboard}**.")
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="board_description", description="Short description text above rankings on the embed")
    async def settings_board_description(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(str, "Description (empty to clear)", required=False, default=None),
    ) -> None:
        if not ctx.guild:
            return
        desc = value.strip() if value and value.strip() else None
        r = await _patch_leaderboard_settings(ctx.guild.id, leaderboard, {"description": desc}, undo_ctx=ctx)
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"Board description updated for **{leaderboard}**.")
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="icon_url", description="Thumbnail image URL for the leaderboard embed")
    async def settings_icon_url(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(str, "Image URL (empty to clear)", required=False, default=None),
    ) -> None:
        if not ctx.guild:
            return
        url = value.strip() if value and value.strip() else None
        r = await _patch_leaderboard_settings(ctx.guild.id, leaderboard, {"icon_url": url}, undo_ctx=ctx)
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"Icon URL updated for **{leaderboard}**.")
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="banner_url", description="Large image URL on the leaderboard embed")
    async def settings_banner_url(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(str, "Image URL (empty to clear)", required=False, default=None),
    ) -> None:
        if not ctx.guild:
            return
        url = value.strip() if value and value.strip() else None
        r = await _patch_leaderboard_settings(ctx.guild.id, leaderboard, {"banner_url": url}, undo_ctx=ctx)
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"Banner URL updated for **{leaderboard}**.")
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="primary_color", description="Embed accent color as hex, e.g. C81E1E")
    async def settings_primary_color(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        hex_color: Option(str, "Hex RRGGBB (empty for default gold)", required=False, default=None),
    ) -> None:
        if not ctx.guild:
            return
        col = _parse_hex_color(hex_color)
        r = await _patch_leaderboard_settings(ctx.guild.id, leaderboard, {"primary_color": col}, undo_ctx=ctx)
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"Primary color set for **{leaderboard}**." + (f" (`#{col:06x}`)" if col is not None else " (default)"))
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="secondary_color", description="Reserved for future UI (stored; not used in embed yet)")
    async def settings_secondary_color(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        hex_color: Option(str, "Hex RRGGBB (empty to clear)", required=False, default=None),
    ) -> None:
        if not ctx.guild:
            return
        col = _parse_hex_color(hex_color)
        r = await _patch_leaderboard_settings(ctx.guild.id, leaderboard, {"secondary_color": col}, undo_ctx=ctx)
        if not r:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        await ctx.respond(f"Secondary color stored for **{leaderboard}**.")
        await self._refresh_rankings_after_settings(ctx, leaderboard)

    # --- Leaderboard commands ---

    @leaderboard_group.command(name="create", description="Create a new leaderboard (optional ELO settings)")
    async def create(
        self,
        ctx: discord.ApplicationContext,
        name: Option(str, "Leaderboard name", required=True),
        match_format: Option(
            str,
            "1v1 duels or 2v2 teams (2v2 uses team average ELO; no archetype AA update)",
            choices=["1v1", "2v2"],
            required=False,
            default="1v1",
        ),
        default_rating: Option(float, "Starting ELO", required=False, default=None),
        k_factor: Option(float, "K factor", required=False, default=None),
        precision: Option(int, "Decimal places (0 = integers)", required=False, default=None),
        loss_dampen: Option(float, "Loser penalty multiplier (0–1)", required=False, default=None),
        max_advantage: Option(float, "Cap per match (0 = unlimited)", required=False, default=None),
        curve_factor: Option(float, "Logistic base (usually 10)", required=False, default=None),
        influence_range: Option(float, "Denominator in formula (often 400)", required=False, default=None),
        ffa_distribution: Option(float, "FFA pairwise scaling", required=False, default=None),
        matches_required: Option(int, "Min matches to show on board", required=False, default=None),
        inactive_days: Option(int, "Hide after N days without a match (0=never)", required=False, default=None),
        locked: Option(bool, "Start locked (no matches)", required=False, default=None),
        cap_range: Option(float, "Gap cap (0 = use influence range)", required=False, default=None),
        cap_favorite_win: Option(bool, "Cap when favorite wins", required=False, default=None),
        cap_favorite_loss: Option(bool, "Cap on upset (favorite loses)", required=False, default=None),
        cap_underdog_win: Option(bool, "Cap when underdog wins", required=False, default=None),
        cap_underdog_loss: Option(bool, "Cap when underdog loses to favorite", required=False, default=None),
        display_title: Option(str, "Embed title override", required=False, default=None),
        board_description: Option(str, "Text above rankings", required=False, default=None),
        icon_url: Option(str, "Thumbnail URL", required=False, default=None),
        banner_url: Option(str, "Banner image URL", required=False, default=None),
        primary_color_hex: Option(str, "Primary color hex RRGGBB", required=False, default=None),
        secondary_color_hex: Option(str, "Secondary color hex RRGGBB", required=False, default=None),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        d = EloSettings().to_dict()
        if default_rating is not None:
            d["default_rating"] = float(default_rating)
        if k_factor is not None:
            d["k_factor"] = float(k_factor)
        if precision is not None:
            d["precision"] = int(precision)
        if loss_dampen is not None:
            d["loss_dampen"] = float(loss_dampen)
        if max_advantage is not None:
            d["max_advantage"] = float(max_advantage)
        if curve_factor is not None:
            d["curve_factor"] = float(curve_factor)
        if influence_range is not None:
            d["influence_range"] = float(influence_range)
        if ffa_distribution is not None:
            d["ffa_distribution"] = float(ffa_distribution)
        if matches_required is not None:
            d["matches_required_for_ranking"] = int(matches_required)
        if inactive_days is not None:
            d["inactive_days_threshold"] = int(inactive_days)
        if locked is not None:
            d["locked"] = bool(locked)
        if cap_range is not None:
            d["cap_range"] = float(cap_range)
        if cap_favorite_win is not None:
            d["cap_favorite_win_impact"] = bool(cap_favorite_win)
        if cap_favorite_loss is not None:
            d["cap_favorite_loss_impact"] = bool(cap_favorite_loss)
        if cap_underdog_win is not None:
            d["cap_underdog_win_impact"] = bool(cap_underdog_win)
        if cap_underdog_loss is not None:
            d["cap_underdog_loss_impact"] = bool(cap_underdog_loss)
        if display_title is not None and str(display_title).strip():
            d["display_title"] = str(display_title).strip()
        elif display_title is not None:
            d["display_title"] = None
        if board_description is not None and str(board_description).strip():
            d["description"] = str(board_description).strip()
        elif board_description is not None:
            d["description"] = None
        if icon_url is not None and str(icon_url).strip():
            d["icon_url"] = str(icon_url).strip()
        elif icon_url is not None:
            d["icon_url"] = None
        if banner_url is not None and str(banner_url).strip():
            d["banner_url"] = str(banner_url).strip()
        elif banner_url is not None:
            d["banner_url"] = None
        pc = _parse_hex_color(primary_color_hex)
        if primary_color_hex is not None:
            d["primary_color"] = pc
        sc = _parse_hex_color(secondary_color_hex)
        if secondary_color_hex is not None:
            d["secondary_color"] = sc

        settings = EloSettings.from_dict(d)
        mf = match_format if match_format in ("1v1", "2v2") else "1v1"
        lb_id = await db.create_leaderboard(guild_id, name, settings, match_format=mf)
        if lb_id:
            await ctx.respond(
                f"Created **{mf}** leaderboard **{name}**. "
                f"Add members with `/leaderboard add`, then use `/leaderboard match` or `/leaderboard match_2v2`."
            )
        else:
            await ctx.respond(f"A leaderboard named **{name}** already exists.", ephemeral=True)

    @leaderboard_group.command(name="list", description="List all leaderboards")
    async def list_boards(self, ctx: discord.ApplicationContext) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        boards = await db.list_leaderboards(guild_id)
        if not boards:
            await ctx.respond("No leaderboards yet. Create one with `/leaderboard create`.")
            return
        lines = [f"• **{name}** (`{fmt}`)" for _lid, name, fmt in boards]
        embed = discord.Embed(title="Leaderboards", description="\n".join(lines), color=0x1E90FF)
        await ctx.respond(embed=embed)

    @leaderboard_group.command(name="add", description="Add yourself to a leaderboard")
    async def add(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        display_name: Option(str, "Optional display name", required=False),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        if not ctx.author:
            await ctx.respond("Could not identify you.", ephemeral=True)
            return
        lb_id = await db.get_leaderboard_id(guild_id, _parse_leaderboard_name_from_autocomplete(leaderboard))
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        ok = await db.add_member(lb_id, ctx.author.id, display_name)
        if ok:
            settings = await db.get_leaderboard_settings(lb_id)
            await ctx.respond(f"Added you to **{leaderboard}** with starting ELO {format_elo(settings.default_rating, settings.precision)}.")
            await self.refresh_rankings_displays(guild_id, lb_id)
        else:
            await ctx.respond(f"You are already on **{leaderboard}**.", ephemeral=True)

    @leaderboard_group.command(name="match", description="Record a match (winner vs loser, with deck names)")
    async def match(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        winner: Option(discord.Member, "Winner (member)", required=True),
        loser: Option(discord.Member, "Loser (member)", required=True),
        winner_deck: Option(str, "Deck/archetype the winner used (e.g. Salamangreat)", required=True),
        loser_deck: Option(str, "Deck/archetype the loser used", required=True),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        lb_id = await db.get_leaderboard_id(guild_id, _parse_leaderboard_name_from_autocomplete(leaderboard))
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        wmem = await db.get_member_entry(lb_id, winner.id)
        lmem = await db.get_member_entry(lb_id, loser.id)
        if not wmem or not lmem:
            await ctx.respond(
                "One or both members are not on this leaderboard. They must use `/leaderboard add` first.",
                ephemeral=True,
            )
            return
        st = await db.get_leaderboard_settings(lb_id)
        if st.locked:
            await ctx.respond(
                f"**{leaderboard}** is locked — no new matches can be recorded until a moderator unlocks it.",
                ephemeral=True,
            )
            return
        meta = await db.get_leaderboard_by_id(lb_id)
        if meta and meta.get("match_format", "1v1") != "1v1":
            await ctx.respond(
                "This leaderboard is **2v2**. Use `/leaderboard match_2v2` with two winners and two losers.",
                ephemeral=True,
            )
            return

        ok = await db.record_match(
            guild_id,
            lb_id,
            winner.id,
            loser.id,
            winner_deck,
            loser_deck,
            actor_id=ctx.author.id,
        )
        if ok:
            await ctx.respond(
                f"Recorded: **{winner.display_name}** ({winner_deck}) defeated **{loser.display_name}** ({loser_deck}) on **{leaderboard}**. "
                "Player ELO updated; archetype AA-ELO updated (1v1)."
            )
            await self.refresh_rankings_displays(guild_id, lb_id)
            await self.refresh_tierlist_displays(guild_id)
        else:
            await ctx.respond("Could not record the match (try again).", ephemeral=True)

    @leaderboard_group.command(
        name="match_2v2",
        description="Record a 2v2 match (team average ELO; no archetype AA-ELO update)",
    )
    async def match_2v2(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "2v2 leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        winner1: Option(discord.Member, "Winning team — player 1", required=True),
        winner2: Option(discord.Member, "Winning team — player 2", required=True),
        loser1: Option(discord.Member, "Losing team — player 1", required=True),
        loser2: Option(discord.Member, "Losing team — player 2", required=True),
        winner1_deck: Option(str, "Winner 1 deck", required=True),
        winner2_deck: Option(str, "Winner 2 deck", required=True),
        loser1_deck: Option(str, "Loser 1 deck", required=True),
        loser2_deck: Option(str, "Loser 2 deck", required=True),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        lb_id = await db.get_leaderboard_id(guild_id, _parse_leaderboard_name_from_autocomplete(leaderboard))
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        meta = await db.get_leaderboard_by_id(lb_id)
        if not meta or meta.get("match_format", "1v1") != "2v2":
            await ctx.respond(
                "This leaderboard is not a **2v2** board. Create one with `/leaderboard create` (format 2v2) or use `/leaderboard match`.",
                ephemeral=True,
            )
            return
        st = await db.get_leaderboard_settings(lb_id)
        if st.locked:
            await ctx.respond(
                f"**{leaderboard}** is locked — no new matches can be recorded until a moderator unlocks it.",
                ephemeral=True,
            )
            return
        ids = {winner1.id, winner2.id, loser1.id, loser2.id}
        if len(ids) < 4:
            await ctx.respond("All **four** players must be different members.", ephemeral=True)
            return
        for m in (winner1, winner2, loser1, loser2):
            if not await db.get_member_entry(lb_id, m.id):
                await ctx.respond(
                    f"**{m.display_name}** is not on this leaderboard — they must `/leaderboard add` first.",
                    ephemeral=True,
                )
                return

        ok = await db.record_match_2v2(
            guild_id,
            lb_id,
            winner1.id,
            winner2.id,
            loser1.id,
            loser2.id,
            winner1_deck,
            winner2_deck,
            loser1_deck,
            loser2_deck,
            actor_id=ctx.author.id,
        )
        if ok:
            await ctx.respond(
                f"Recorded 2v2 on **{leaderboard}**: **{winner1.display_name}**/**{winner2.display_name}** "
                f"({winner1_deck} / {winner2_deck}) defeated **{loser1.display_name}**/**{loser2.display_name}** "
                f"({loser1_deck} / {loser2_deck}). Player ELO updated (team average); archetype AA-ELO unchanged."
            )
            await self.refresh_rankings_displays(guild_id, lb_id)
        else:
            await ctx.respond("Could not record the 2v2 match.", ephemeral=True)

    @leaderboard_group.command(name="undo", description="Undo the last leaderboard match or settings change (Mod/Admin)")
    async def leaderboard_undo(self, ctx: discord.ApplicationContext) -> None:
        if not ctx.guild or not ctx.author:
            return
        if not _mod_perms(ctx.author):
            await ctx.respond("You need **Moderator** or **Administrator** permissions.", ephemeral=True)
            return
        ok, msg, hint = await db.pop_and_apply_undo(ctx.guild.id)
        if not ok:
            await ctx.respond(msg, ephemeral=True)
            return
        await ctx.respond(msg)
        lid = int(hint["leaderboard_id"]) if hint.get("leaderboard_id") else 0
        if lid > 0:
            await self.refresh_rankings_displays(ctx.guild.id, lid)
        if hint.get("refresh_tierlist"):
            await self.refresh_tierlist_displays(ctx.guild.id)

    @leaderboard_group.command(name="view", description="View the member ELO leaderboard")
    async def view(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=False, autocomplete=leaderboard_autocomplete),
    ) -> None:
        await self._respond_rankings(ctx, leaderboard)

    @leaderboard_group.command(name="rankings", description="Display member ELO rankings (same as /leaderboard view)")
    async def rankings(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=False, autocomplete=leaderboard_autocomplete),
    ) -> None:
        await self._respond_rankings(ctx, leaderboard)

    @leaderboard_group.command(name="tierlist", description="View archetype tier list (deck strength by win-rate)")
    async def tierlist(
        self,
        ctx: discord.ApplicationContext,
    ) -> None:
        await self._respond_tierlist(ctx)

    @leaderboard_group.command(name="tiers", description="Display archetype tier list (same as /leaderboard tierlist)")
    async def tiers(
        self,
        ctx: discord.ApplicationContext,
    ) -> None:
        await self._respond_tierlist(ctx)

    @leaderboard_group.command(name="customize", description="Set your display name on a leaderboard")
    async def customize(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        display_name: Option(str, "Display name to show on leaderboard", required=True),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        if not ctx.author:
            await ctx.respond("Could not identify you.", ephemeral=True)
            return
        lb_id = await db.get_leaderboard_id(guild_id, _parse_leaderboard_name_from_autocomplete(leaderboard))
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        ok = await db.set_member_display_name(lb_id, ctx.author.id, display_name)
        if ok:
            await ctx.respond(f"Your display name on **{leaderboard}** is now **{display_name}**.")
            await self.refresh_rankings_displays(guild_id, lb_id)
        else:
            await ctx.respond(f"You are not on **{leaderboard}**. Add yourself first with `/leaderboard add`.", ephemeral=True)

    @leaderboard_group.command(name="remove", description="Remove yourself from a leaderboard")
    async def remove(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        if not ctx.author:
            await ctx.respond("Could not identify you.", ephemeral=True)
            return
        lb_id = await db.get_leaderboard_id(guild_id, _parse_leaderboard_name_from_autocomplete(leaderboard))
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        ok = await db.remove_member(lb_id, ctx.author.id)
        if ok:
            await ctx.respond(f"Removed you from **{leaderboard}**.")
            await self.refresh_rankings_displays(guild_id, lb_id)
        else:
            await ctx.respond(f"You are not on **{leaderboard}**.", ephemeral=True)

    @leaderboard_group.command(name="reset", description="Reset all ELOs on a leaderboard (Mod/Admin only)")
    async def reset_leaderboard_cmd(
        self,
        ctx: discord.ApplicationContext,
        name: Option(str, "Leaderboard to reset", required=True, autocomplete=leaderboard_autocomplete),
    ) -> None:
        if not ctx.author or not ctx.guild:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        perms = ctx.author.guild_permissions
        if not (perms.administrator or perms.manage_guild or perms.manage_messages):
            await ctx.respond("You need Moderator or Administrator role.", ephemeral=True)
            return
        ok = await db.reset_leaderboard(ctx.guild.id, name)
        if ok:
            await ctx.respond(f"Reset **{name}** — all member ELOs set to default.")
            lid = await db.get_leaderboard_id(ctx.guild.id, name)
            if lid:
                await self.refresh_rankings_displays(ctx.guild.id, lid)
        else:
            await ctx.respond(f"No leaderboard named **{name}** found.", ephemeral=True)

    @leaderboard_group.command(name="reset_tierlist", description="Reset archetype tier list (Mod/Admin only)")
    async def reset_tierlist_cmd(self, ctx: discord.ApplicationContext) -> None:
        if not ctx.author or not ctx.guild:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        perms = ctx.author.guild_permissions
        if not (perms.administrator or perms.manage_guild or perms.manage_messages):
            await ctx.respond("You need Moderator or Administrator role.", ephemeral=True)
            return
        await db.reset_tier_list(ctx.guild.id)
        await ctx.respond("Tier list reset — all archetype ELOs set to 1000.")
        await self.refresh_tierlist_displays(ctx.guild.id)

    @leaderboard_group.command(name="delete", description="Delete an entire leaderboard (Mod/Admin only)")
    async def delete_board(
        self,
        ctx: discord.ApplicationContext,
        name: Option(str, "Leaderboard to delete", required=True, autocomplete=leaderboard_autocomplete),
    ) -> None:
        if not ctx.author or not ctx.guild:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        perms = ctx.author.guild_permissions
        if not (perms.administrator or perms.manage_guild or perms.manage_messages):
            await ctx.respond("You need Moderator or Administrator role.", ephemeral=True)
            return
        lb_id = await db.get_leaderboard_id(ctx.guild.id, name)
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{name}** found.", ephemeral=True)
            return
        pairs = await db.delete_live_rankings_for_leaderboard(ctx.guild.id, lb_id)
        for ch_id, msg_id in pairs:
            ch = ctx.guild.get_channel(ch_id)
            if ch and isinstance(ch, discord.TextChannel):
                try:
                    m = await ch.fetch_message(msg_id)
                    await m.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        ok = await db.delete_leaderboard(ctx.guild.id, name)
        if ok:
            await ctx.respond(f"Deleted leaderboard **{name}** and removed its live display messages.")
        else:
            await ctx.respond(f"No leaderboard named **{name}** found.", ephemeral=True)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(LeaderboardCog(bot))
