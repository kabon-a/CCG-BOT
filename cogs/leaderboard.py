"""Leaderboard cog - ELO ranking for Yu-Gi-Oh! card names."""

import discord
from discord import Option
from discord.ext import commands

import database as db


async def leaderboard_autocomplete(ctx: discord.AutocompleteContext) -> list[str]:
    """Autocomplete for leaderboard names in this guild."""
    if not ctx.interaction.guild_id:
        return []
    leaderboards = await db.list_leaderboards(ctx.interaction.guild_id)
    return [name for _, name in leaderboards]


class LeaderboardCog(commands.Cog):
    """ELO leaderboard commands for Yu-Gi-Oh! card names."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    leaderboard_group = discord.SlashCommandGroup("leaderboard", "ELO leaderboard for Yu-Gi-Oh! card names")

    @leaderboard_group.command(name="create", description="Create a new leaderboard")
    async def create(
        self,
        ctx: discord.ApplicationContext,
        name: Option(str, "Leaderboard name", required=True),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        lb_id = await db.create_leaderboard(guild_id, name)
        if lb_id:
            await ctx.respond(f"Created leaderboard **{name}**. Add cards with `/leaderboard add`.")
        else:
            await ctx.respond(f"A leaderboard named **{name}** already exists.", ephemeral=True)

    @leaderboard_group.command(name="list", description="List all leaderboards in this server")
    async def list_boards(self, ctx: discord.ApplicationContext) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        boards = await db.list_leaderboards(guild_id)
        if not boards:
            await ctx.respond("No leaderboards yet. Create one with `/leaderboard create`.")
            return
        lines = [f"• **{name}**" for _, name in boards]
        embed = discord.Embed(title="Leaderboards", description="\n".join(lines), color=0x1E90FF)
        await ctx.respond(embed=embed)

    @leaderboard_group.command(name="add", description="Add a Yu-Gi-Oh! card to a leaderboard")
    async def add(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        card_name: Option(str, "Card name (Yu-Gi-Oh! card)", required=True),
        display_name: Option(str, "Custom name shown on leaderboard", required=False),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        lb_id = await db.get_leaderboard_id(guild_id, leaderboard)
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{leaderboard}**. Use `/leaderboard list` to see names.", ephemeral=True)
            return
        ok = await db.add_entry(lb_id, card_name, display_name)
        if ok:
            display = (display_name or card_name).strip()
            await ctx.respond(f"Added **{display}** to **{leaderboard}** with starting ELO 1000.")
        else:
            await ctx.respond(f"**{card_name}** is already on **{leaderboard}**.", ephemeral=True)

    @leaderboard_group.command(name="match", description="Record a match result (winner vs loser)")
    async def match(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        winner: Option(str, "Card that won", required=True),
        loser: Option(str, "Card that lost", required=True),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        lb_id = await db.get_leaderboard_id(guild_id, leaderboard)
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        ok = await db.record_match(lb_id, winner, loser)
        if ok:
            await ctx.respond(f"Recorded: **{winner}** defeated **{loser}** on **{leaderboard}**. ELO updated.")
        else:
            await ctx.respond("One or both cards not found on this leaderboard.", ephemeral=True)

    @leaderboard_group.command(name="customize", description="Change the display name of a card on the leaderboard")
    async def customize(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        card_name: Option(str, "Card to rename", required=True),
        display_name: Option(str, "New name to show on leaderboard", required=True),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        lb_id = await db.get_leaderboard_id(guild_id, leaderboard)
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        ok = await db.set_display_name(lb_id, card_name, display_name)
        if ok:
            await ctx.respond(f"Updated **{card_name}** to display as **{display_name}** on **{leaderboard}**.")
        else:
            await ctx.respond(f"**{card_name}** not found on **{leaderboard}**.", ephemeral=True)

    @leaderboard_group.command(name="view", description="View the ELO leaderboard")
    async def view(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=False, autocomplete=leaderboard_autocomplete),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        boards = await db.list_leaderboards(guild_id)
        if not boards:
            await ctx.respond("No leaderboards yet. Create one with `/leaderboard create`.")
            return

        if leaderboard:
            lb_id = await db.get_leaderboard_id(guild_id, leaderboard)
            if not lb_id:
                await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
                return
            entries = await db.get_leaderboard(lb_id)
            title = f"📊 {leaderboard} — ELO Leaderboard"
        else:
            # Show first board by default
            lb_id, lb_name = boards[0]
            entries = await db.get_leaderboard(lb_id)
            title = f"📊 {lb_name} — ELO Leaderboard"

        if not entries:
            await ctx.respond("This leaderboard has no entries yet. Add cards with `/leaderboard add`.")
            return

        lines = []
        for i, (_, display, elo) in enumerate(entries, 1):
            medal = {"1": "🥇", "2": "🥈", "3": "🥉"}.get(str(i), f"`{i}.`")
            lines.append(f"{medal} **{display}** — {int(elo)} ELO")
        embed = discord.Embed(title=title, description="\n".join(lines), color=0xFFD700)
        await ctx.respond(embed=embed)

    @leaderboard_group.command(name="remove", description="Remove a card from a leaderboard")
    async def remove(
        self,
        ctx: discord.ApplicationContext,
        leaderboard: Option(str, "Leaderboard name", required=True, autocomplete=leaderboard_autocomplete),
        card_name: Option(str, "Card to remove", required=True),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        lb_id = await db.get_leaderboard_id(guild_id, leaderboard)
        if not lb_id:
            await ctx.respond(f"No leaderboard named **{leaderboard}**.", ephemeral=True)
            return
        ok = await db.remove_entry(lb_id, card_name)
        if ok:
            await ctx.respond(f"Removed **{card_name}** from **{leaderboard}**.")
        else:
            await ctx.respond(f"**{card_name}** not found on **{leaderboard}**.", ephemeral=True)

    @leaderboard_group.command(name="delete", description="Delete an entire leaderboard")
    async def delete_board(
        self,
        ctx: discord.ApplicationContext,
        name: Option(str, "Leaderboard to delete", required=True, autocomplete=leaderboard_autocomplete),
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else 0
        ok = await db.delete_leaderboard(guild_id, name)
        if ok:
            await ctx.respond(f"Deleted leaderboard **{name}**.")
        else:
            await ctx.respond(f"No leaderboard named **{name}** found.", ephemeral=True)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(LeaderboardCog(bot))
