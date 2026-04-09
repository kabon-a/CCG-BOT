"""Leaderboard cog - ELO ranking for members + archetype tier list."""

import discord
from discord import Option
from discord.ext import commands

import database as db
from database import EloSettings, format_elo


async def leaderboard_autocomplete(ctx: discord.AutocompleteContext) -> list[str]:
    if not ctx.interaction.guild_id:
        return []
    boards = await db.list_leaderboards(ctx.interaction.guild_id)
    return [name for _, name in boards]


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
        if not entries:
            desc = "*No players on this leaderboard yet.*"
        else:
            lines = []
            for i, (uid, disp, elo) in enumerate(entries, 1):
                medal = {"1": "🥇", "2": "🥈", "3": "🥉"}.get(str(i), f"`{i}.`")
                name = disp or f"<@{uid}>"
                lines.append(f"{medal} **{name}** — {format_elo(elo, settings.precision)} ELO")
            desc = "\n".join(lines)
        embed = discord.Embed(
            title=f"📊 {lb_name} — ELO Leaderboard",
            description=desc,
            color=0xFFD700,
        )
        embed.set_footer(text="Live • Updates when matches, roster, or settings change")
        return embed

    async def _build_tierlist_embed(self, guild_id: int) -> discord.Embed:
        entries = await db.get_tier_list(guild_id)
        if not entries:
            desc = "*No archetypes recorded yet. Record matches with deck names to build the tier list.*"
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
        embed.set_footer(text="Live • Updates when match results or tier list reset")
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
            lb_id = await db.get_leaderboard_id(guild_id, leaderboard)
            if not lb_id:
                await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
                return
        else:
            lb_id, _ = boards[0]

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

    # --- ELO Settings ---

    @settings_group.command(name="view", description="View current ELO settings for a leaderboard")
    async def settings_view(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        lb_id = await db.get_leaderboard_id(guild_id, leaderboard)
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        s = await db.get_leaderboard_settings(lb_id)
        embed = discord.Embed(
            title=f"⚙️ ELO Settings — {leaderboard}",
            color=0x1E90FF,
        )
        embed.add_field(name="Basic", value=(
            f"**Default Rating:** {s.default_rating}\n"
            f"**K Factor:** {s.k_factor}\n"
            f"**Precision:** {s.precision} decimal(s)\n"
            f"**Loss Dampen:** {s.loss_dampen}"
        ), inline=True)
        embed.add_field(name="Curve", value=(
            f"**Max Advantage:** {s.max_advantage}\n"
            f"**Curve Factor:** {s.curve_factor}\n"
            f"**Influence Range:** {s.influence_range}\n"
            f"**FFA Distribution:** {s.ffa_distribution}"
        ), inline=True)
        await ctx.respond(embed=embed)

    async def _refresh_rankings_after_settings(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: str,
    ) -> None:
        if not ctx.guild:
            return
        lid = await db.get_leaderboard_id(ctx.guild.id, leaderboard)
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
        lb_id = await db.get_leaderboard_id(guild_id, leaderboard)
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
        else:
            d[key] = float(value)
        s = EloSettings.from_dict(d)
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

    @settings_group.command(name="max_advantage", description="Cap max ELO gain/loss per game")
    async def set_max_advantage(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(float, "Max advantage", required=True),
    ) -> None:
        if await self._set_setting(ctx, leaderboard, "max_advantage", value, 1, 100):
            await ctx.respond(f"Max advantage set to **{value}** for **{leaderboard}**.")
            await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="curve_factor", description="Curve factor (400 = standard)")
    async def set_curve_factor(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(float, "Curve factor", required=True),
    ) -> None:
        if await self._set_setting(ctx, leaderboard, "curve_factor", value, 100, 1000):
            await ctx.respond(f"Curve factor set to **{value}** for **{leaderboard}**.")
            await self._refresh_rankings_after_settings(ctx, leaderboard)

    @settings_group.command(name="influence_range", description="Influence range")
    async def set_influence_range(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        value: Option(float, "Influence range", required=True),
    ) -> None:
        if await self._set_setting(ctx, leaderboard, "influence_range", value, 100, 1000):
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

    # --- Leaderboard commands ---

    @leaderboard_group.command(name="create", description="Create a new leaderboard")
    async def create(
        self,
        ctx: discord.ApplicationContext,
        name: Option(str, "Leaderboard name", required=True),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        lb_id = await db.create_leaderboard(guild_id, name)
        if lb_id:
            await ctx.respond(f"Created leaderboard **{name}**. Add members with `/leaderboard add`.")
        else:
            await ctx.respond(f"A leaderboard named **{name}** already exists.", ephemeral=True)

    @leaderboard_group.command(name="list", description="List all leaderboards")
    async def list_boards(self, ctx: discord.ApplicationContext) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        boards = await db.list_leaderboards(guild_id)
        if not boards:
            await ctx.respond("No leaderboards yet. Create one with `/leaderboard create`.")
            return
        lines = [f"• **{name}**" for _, name in boards]
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
        lb_id = await db.get_leaderboard_id(guild_id, leaderboard)
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
        lb_id = await db.get_leaderboard_id(guild_id, leaderboard)
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        ok = await db.record_match(guild_id, lb_id, winner.id, loser.id, winner_deck, loser_deck)
        if ok:
            await ctx.respond(
                f"Recorded: **{winner.display_name}** ({winner_deck}) defeated **{loser.display_name}** ({loser_deck}) on **{leaderboard}**. "
                "Member ELO and archetype tier list updated."
            )
            await self.refresh_rankings_displays(guild_id, lb_id)
            await self.refresh_tierlist_displays(guild_id)
        else:
            await ctx.respond("One or both members are not on this leaderboard. They must use `/leaderboard add` first.", ephemeral=True)

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
        lb_id = await db.get_leaderboard_id(guild_id, leaderboard)
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
        lb_id = await db.get_leaderboard_id(guild_id, leaderboard)
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
