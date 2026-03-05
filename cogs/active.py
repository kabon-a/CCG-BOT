"""Active cog - Assign @active role to users with recent activity (within 7 days)."""

import discord
from discord.ext import commands, tasks

import database as db


ACTIVE_ROLE_NAME = "active"


def setup(bot: commands.Bot) -> None:
    bot.add_cog(ActiveCog(bot))


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


async def grant_active_and_record(guild: discord.Guild, user: discord.Member | discord.User) -> None:
    """Grant @active role and record activity. Idempotent."""
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


async def _remove_stale_active_impl(bot: commands.Bot) -> None:
    """Remove @active from users with no activity in 7+ days."""
    for guild in bot.guilds:
        role = discord.utils.get(guild.roles, name=ACTIVE_ROLE_NAME)
        if not role:
            continue
        to_remove = await db.get_user_ids_to_remove_active(guild.id)
        for uid in to_remove:
            member = guild.get_member(uid)
            if member and role in member.roles:
                try:
                    await member.remove_roles(role, reason="No activity in 7 days")
                except discord.Forbidden:
                    pass


class ActiveCog(commands.Cog):
    """Assigns @active to users with recent activity. Removes from inactive users."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @tasks.loop(hours=24)
    async def cleanup_stale_active(self) -> None:
        await _remove_stale_active_impl(self.bot)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.cleanup_stale_active.is_running():
            self.cleanup_stale_active.start()

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
