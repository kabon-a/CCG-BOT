"""Announce cog - Mod/Admin send messages through the bot."""

import discord
from deep_translator import GoogleTranslator
from discord import Option
from discord.ext import commands
from langdetect import LangDetectException, detect

import database as db


class AnnounceCog(commands.Cog):
    """Commands for moderators to send messages through the bot."""

    async def _translate(self, text: str, source_lang: str, target_lang: str) -> str | None:
        try:
            if source_lang == target_lang:
                return text
            translated = GoogleTranslator(source=source_lang, target=target_lang).translate(text)
            if translated and translated.strip():
                return translated.strip()
            return None
        except Exception:
            return None

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
            # Base announcement in original text
            await target.send(message)

            # Audience-aware language block (Discord cannot render per-viewer message variants).
            guild_langs = await db.get_guild_first_languages(ctx.guild.id)
            # Include everyone else as English by default
            lang_counts: dict[str, int] = {}
            for m in ctx.guild.members:
                if m.bot:
                    continue
                lang = guild_langs.get(m.id, "en")
                lang_counts[lang] = lang_counts.get(lang, 0) + 1

            try:
                detected = detect(message)
            except LangDetectException:
                detected = "auto"
            except Exception:
                detected = "auto"

            # Keep output concise: prioritize major language groups.
            ordered_langs = sorted(lang_counts.items(), key=lambda kv: kv[1], reverse=True)
            top_langs = [lang for lang, _ in ordered_langs[:6]]
            if "en" not in top_langs:
                top_langs.append("en")

            translations: list[str] = []
            for lang in top_langs:
                translated = await self._translate(message, detected, lang) if detected != "auto" else None
                if translated:
                    translations.append(f"**{lang.upper()}**: {translated}")

            if translations:
                embed = discord.Embed(
                    title="🌐 Announcement Translations",
                    description="\n\n".join(translations),
                    color=0x1E90FF,
                )
                embed.set_footer(text="Viewer-specific rendering is not supported by Discord; translations are grouped by language.")
                await target.send(embed=embed)

            await ctx.respond(f"Message sent to {target.mention}.", ephemeral=True)
        except discord.Forbidden:
            await ctx.respond("I don't have permission to send messages in that channel.", ephemeral=True)
        except discord.HTTPException as e:
            await ctx.respond(f"Failed to send: {e}", ephemeral=True)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(AnnounceCog(bot))
