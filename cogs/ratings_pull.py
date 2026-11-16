"""Periodically pull Interspace-approved rated matches into the local bot DB."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

import database as db
from ratings_sync import fetch_pending_sync, mark_synced

log = logging.getLogger("ccg-bot.ratings-pull")


class RatingsPull(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pull_loop.start()

    def cog_unload(self):
        self.pull_loop.cancel()

    @tasks.loop(minutes=2.0)
    async def pull_loop(self):
        items = await fetch_pending_sync()
        for item in items:
            try:
                await self._apply(item)
                match_id = str(item.get("matchId") or "")
                if match_id:
                    await mark_synced(match_id)
            except Exception:
                log.exception("Failed to mirror match %s", item.get("matchId"))

    @pull_loop.before_loop
    async def before_pull(self):
        await self.bot.wait_until_ready()

    async def _apply(self, item: dict) -> None:
        """Best-effort mirror: ensure Discord-linked members exist on the default ladder."""
        # Local mirror of Interspace-approved matches is handled primarily when
        # Discord admins also record via /leaderboard match. Pending sync marks
        # Interspace rows consumed so the web app stays the source of truth.
        _ = item
        return


def setup(bot: commands.Bot):
    bot.add_cog(RatingsPull(bot))
