"""CCG ELO Bot - Yu-Gi-Oh! card name leaderboards."""

import asyncio
import discord
from discord.ext import commands

import database as db
from config import BOT_TOKEN

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # Required for /leaderboard match (Member option)

bot = commands.Bot(intents=intents)


@bot.event
async def on_ready() -> None:
    await db.init_db()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Ready.")


def main() -> None:
    asyncio.run(db.init_db())
    bot.load_extension("cogs.leaderboard")
    bot.load_extension("cogs.announce")
    bot.load_extension("cogs.active")
    bot.load_extension("cogs.poll")
    bot.load_extension("cogs.translate")
    # Discord ↔ Interspace account linking (/discord_link, /discord_unlink)
    bot.load_extension("cogs.link")
    # Posts published card images to the card-releases channel.
    bot.load_extension("cogs.card_releases")
    # PSCT lint reports from #report-a-problem → Cursor cloud agent → PR
    bot.load_extension("cogs.psct_report")
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
