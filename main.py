"""CCG ELO Bot - Yu-Gi-Oh! card name leaderboards."""

import discord
from discord.ext import commands

import database as db
from config import BOT_TOKEN


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(intents=intents)


@bot.event
async def on_ready() -> None:
    await db.init_db()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Ready.")


def main() -> None:
    bot.load_extension("cogs.leaderboard")
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
