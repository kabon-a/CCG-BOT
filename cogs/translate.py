"""Translate cog - per-user auto translate mode with temporary messages."""

import asyncio
import re

import discord
from deep_translator import GoogleTranslator
from discord import Option
from discord.ext import commands
from langdetect import LangDetectException, detect

import database as db

LANG_CODE_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z]{2,4})?$", re.IGNORECASE)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(TranslateCog(bot))


class TranslateCog(commands.Cog):
    """Auto-translate incoming messages for users who enable it."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    translate_group = discord.SlashCommandGroup("translate_mode", "Per-user auto-translate settings")
    language_group = discord.SlashCommandGroup("language", "Per-user first language settings")

    @language_group.command(name="set", description="Set your first language for this server (default: en).")
    async def set_first_language(
        self,
        ctx: discord.ApplicationContext,
        lang_code: Option(str, "Language code (e.g. en, es, it, ko, fr)", required=True),
    ) -> None:
        if not ctx.guild or not ctx.author:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        code = lang_code.lower().strip()
        if not LANG_CODE_RE.match(code):
            await ctx.respond("Invalid language code format. Example: `en`, `es`, `fr`, `zh-cn`.", ephemeral=True)
            return
        await db.set_user_first_language(ctx.guild.id, ctx.author.id, code)
        await ctx.respond(f"Your first language is now set to **{code}**.", ephemeral=True)

    @language_group.command(name="status", description="Show your first language for this server.")
    async def first_language_status(self, ctx: discord.ApplicationContext) -> None:
        if not ctx.guild or not ctx.author:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        code = await db.get_user_first_language(ctx.guild.id, ctx.author.id)
        await ctx.respond(f"Your first language is **{code}**.", ephemeral=True)

    @translate_group.command(name="enable", description="Enable auto-translate for your account in this server.")
    async def enable(
        self,
        ctx: discord.ApplicationContext,
        source_lang: Option(str, "Language A code. Leave empty for auto-detect", required=False) = None,
        target_lang: Option(str, "Language B code. Leave empty to use your first language", required=False) = None,
        ttl_seconds: Option(int, "Temporary message lifetime in seconds (5-300)", required=False) = 10,
    ) -> None:
        if not ctx.guild or not ctx.author:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        first_lang = await db.get_user_first_language(ctx.guild.id, ctx.author.id)
        src = (source_lang or "auto").lower().strip()
        tgt = (target_lang or first_lang).lower().strip()
        if src != "auto" and not LANG_CODE_RE.match(src):
            await ctx.respond("Invalid source language code format.", ephemeral=True)
            return
        if not LANG_CODE_RE.match(tgt):
            await ctx.respond("Invalid target language code format.", ephemeral=True)
            return
        if src != "auto" and src == tgt:
            await ctx.respond("Source and target language must be different.", ephemeral=True)
            return
        ttl = max(5, min(300, int(ttl_seconds)))
        await db.upsert_auto_translate_pref(ctx.guild.id, ctx.author.id, src, tgt, ttl, enabled=True)
        await ctx.respond(
            f"Auto-translate enabled: messages detected as **{src}** will be translated to **{tgt}** for you. "
            f"Temporary translation messages delete after {ttl}s.",
            ephemeral=True,
        )

    @translate_group.command(name="disable", description="Disable auto-translate for your account in this server.")
    async def disable(self, ctx: discord.ApplicationContext) -> None:
        if not ctx.guild or not ctx.author:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        ok = await db.disable_auto_translate_pref(ctx.guild.id, ctx.author.id)
        if ok:
            await ctx.respond("Auto-translate disabled.", ephemeral=True)
        else:
            await ctx.respond("Auto-translate was not enabled.", ephemeral=True)

    @translate_group.command(name="status", description="Show your current auto-translate settings.")
    async def status(self, ctx: discord.ApplicationContext) -> None:
        if not ctx.guild or not ctx.author:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        pref = await db.get_auto_translate_pref(ctx.guild.id, ctx.author.id)
        if not pref or not pref.get("enabled"):
            await ctx.respond("Auto-translate is currently disabled.", ephemeral=True)
            return
        await ctx.respond(
            f"Auto-translate is enabled.\n"
            f"- Source: `{pref['source_lang']}`\n"
            f"- Target: `{pref['target_lang']}`\n"
            f"- Temporary message TTL: `{pref['ttl_seconds']}s`",
            ephemeral=True,
        )

    async def _translate_text(self, text: str, source_lang: str, target_lang: str) -> str | None:
        def _run() -> str:
            return GoogleTranslator(source=source_lang, target=target_lang).translate(text)

        try:
            translated = await asyncio.to_thread(_run)
            if translated and translated.strip():
                return translated.strip()
            return None
        except Exception:
            return None

    async def _delete_later(self, message: discord.Message, ttl_seconds: int) -> None:
        await asyncio.sleep(ttl_seconds)
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    @staticmethod
    def _normalize_compare_text(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip().lower()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or not message.author or message.author.bot:
            return
        if not message.content or not message.content.strip():
            return
        # Avoid trying to translate tiny fragments/noise.
        if len(message.content.strip()) < 3:
            return

        prefs = await db.get_enabled_auto_translate_prefs(message.guild.id)
        if not prefs:
            return

        try:
            detected = detect(message.content)
        except LangDetectException:
            return
        except Exception:
            return

        for pref in prefs:
            uid = int(pref["user_id"])
            if uid == message.author.id:
                continue
            source_lang = str(pref["source_lang"]).lower()
            user_first_lang = await db.get_user_first_language(message.guild.id, uid)
            if source_lang == "auto":
                if detected.lower() == user_first_lang:
                    continue
            elif detected.lower() != source_lang:
                continue
            target_lang = str(pref["target_lang"]).lower()
            ttl = int(pref.get("ttl_seconds") or 10)

            # Skip if already detected as target language.
            if detected.lower() == target_lang:
                continue

            translated = await self._translate_text(message.content, source_lang, target_lang)
            if not translated:
                continue
            # Skip no-op translations (common on short English messages).
            if self._normalize_compare_text(translated) == self._normalize_compare_text(message.content):
                continue

            try:
                recipient = message.guild.get_member(uid) or self.bot.get_user(uid)
                if not recipient:
                    continue
                out = await recipient.send(f"@{message.author.display_name}: {translated}")
                asyncio.create_task(self._delete_later(out, ttl))
            except (discord.Forbidden, discord.HTTPException):
                continue
