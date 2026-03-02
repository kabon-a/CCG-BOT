"""Announce cog - Mod/Admin send messages through the bot."""

import discord
from discord import Option
from discord.ext import commands


class AnnounceCog(commands.Cog):
    """Commands for moderators to send messages through the bot."""

    @commands.slash_command(
        name="announce",
        description="Send a message through the bot (Mod/Admin only). Default: current channel.",
    )
    async def announce(
        self,
        ctx: discord.ApplicationContext,
        message: Option(str, "Message to send", required=True),
        channel: Option(
            discord.TextChannel,
            "Channel to send to (default: current channel)",
            required=False,
        ) = None,
    ) -> None:
        if not ctx.author or not ctx.guild:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        perms = ctx.author.guild_permissions
        if not (perms.administrator or perms.manage_guild or perms.manage_messages):
            await ctx.respond("You need Administrator, Manage Server, or Manage Messages permission.", ephemeral=True)
            return

        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            await ctx.respond("Invalid channel.", ephemeral=True)
            return

        try:
            await target.send(message)
            await ctx.respond(f"Message sent to {target.mention}.", ephemeral=True)
        except discord.Forbidden:
            await ctx.respond("I don't have permission to send messages in that channel.", ephemeral=True)
        except discord.HTTPException as e:
            await ctx.respond(f"Failed to send: {e}", ephemeral=True)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(AnnounceCog(bot))
