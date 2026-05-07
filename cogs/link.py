"""Link cog — Discord ↔ Interspace account linking.

Linking flow:
  1. The user generates a one-time code on the Interspace web UI
     (Settings → "Discord Link" → Generate Link Code).
  2. The user runs ``/discord_link <code>`` in this Discord server.
  3. This cog calls the Interspace ``/api/discord/link/complete`` endpoint
     with the bot secret. On success, the user's Discord ID is bound to
     their Interspace masterID.

Once linked:
  * Activity recorded on Discord (messages, reactions, slash commands)
    is also reported to Interspace via ``/api/discord/active-ping`` —
    see ``cogs/active.py``. This keeps the user inside the active-voter
    set Interspace uses for the 65% poll quorum.
  * Activity recorded on Interspace (any authenticated request) is
    pulled back here through ``/api/discord/active-pulse`` and grants
    the @active role on Discord — also see ``cogs/active.py``.

Unlinking:
  * ``/discord_unlink`` — calls the bot-auth endpoint
    ``/api/discord/link/unlink-by-discord-id`` so the user doesn't have
    to roundtrip through Interspace to drop the binding.
"""

import aiohttp
import discord
from discord import Option
from discord.ext import commands

from config import INTERSPACE_URL, INTERSPACE_BOT_SECRET

# Must match Backend/server.js DISCORD_ROLE_MAP keys exactly. Any role not in
# this set is ignored by Interspace's role sync.
INTERSPACE_TRACKED_ROLES = {
    "Creator",
    "Overseer",
    "Overseer in Training",
    "Administrator",
    "Artist",
    "The Format Council",
}
ACTIVE_ROLE_NAME = "active"


def _collect_member_roles(member: discord.Member | None) -> tuple[list[str], bool]:
    """Return (role_names_for_interspace, has_active_role) for a guild member."""
    if not member:
        return [], False
    role_names = [r.name for r in member.roles]
    tracked = [r for r in role_names if r in INTERSPACE_TRACKED_ROLES]
    is_active = ACTIVE_ROLE_NAME in role_names
    return tracked, is_active


def _interspace_headers() -> dict:
    return {"x-bot-secret": INTERSPACE_BOT_SECRET, "Content-Type": "application/json"}


async def _interspace_post(path: str, payload: dict) -> tuple[int, dict | None]:
    """POST to Interspace. Returns (status_code, json_body_or_none).

    Unlike the helper in ``cogs/poll.py`` we surface the status code so
    the slash command can render specific error messages (404 for an
    unknown code, 409 for a Discord ID already bound to another account,
    etc.).
    """
    if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
        return 0, None
    url = f"{INTERSPACE_URL}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=_interspace_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                ct = resp.headers.get("content-type", "")
                if "application/json" in ct:
                    return resp.status, await resp.json()
                return resp.status, {"text": (await resp.text())[:200]}
    except Exception as exc:
        print(f"[Interspace] POST {path} failed: {exc}")
        return 0, None


def setup(bot: commands.Bot) -> None:
    bot.add_cog(LinkCog(bot))


class LinkCog(commands.Cog):
    """Slash commands for binding a Discord user to an Interspace account."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.slash_command(
        name="discord_link",
        description="Link your Discord account to your Interspace account using a one-time code.",
    )
    async def discord_link(
        self,
        ctx: discord.ApplicationContext,
        code: Option(
            str,
            "The 8-character code shown on Interspace → Settings → Discord Link.",
            required=True,
        ),
    ) -> None:
        if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
            await ctx.respond(
                "Interspace integration is not configured on this bot. "
                "Please contact an admin.",
                ephemeral=True,
            )
            return
        if not ctx.author:
            await ctx.respond("Cannot resolve your Discord identity.", ephemeral=True)
            return

        clean_code = (code or "").strip().upper()
        if len(clean_code) < 4:
            await ctx.respond("Please provide the link code from Interspace.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        # Build a stable, human-friendly Discord username string. Modern usernames
        # have no discriminator, so prefer ``name`` and fall back to legacy form.
        discord_username = getattr(ctx.author, "name", str(ctx.author))
        if getattr(ctx.author, "discriminator", "0") not in ("0", "", None):
            discord_username = f"{discord_username}#{ctx.author.discriminator}"

        # Snapshot the user's current Discord roles + @active state so Interspace
        # can grant matching roles immediately on link instead of waiting for the
        # next on_member_update event.
        member = ctx.guild.get_member(ctx.author.id) if ctx.guild else None
        tracked_roles, is_active = _collect_member_roles(member)

        status, body = await _interspace_post(
            "/api/discord/link/complete",
            {
                "code": clean_code,
                "discordId": str(ctx.author.id),
                "discordUsername": discord_username,
                "roles": tracked_roles,
                "isActive": is_active,
            },
        )

        if status == 0:
            await ctx.respond(
                "Could not reach Interspace. Try again in a minute.",
                ephemeral=True,
            )
            return
        if status == 200 and body and body.get("ok"):
            await ctx.respond(
                f"Linked to Interspace user **{body.get('username') or body.get('masterID')}**. "
                f"Your activity on Discord and Interspace now keep each other in sync.",
                ephemeral=True,
            )
            return
        if status == 404:
            await ctx.respond(
                "That code is invalid or expired. Generate a new one on Interspace "
                "(Settings → Discord Link) and try again — codes last 15 minutes.",
                ephemeral=True,
            )
            return
        if status == 409:
            await ctx.respond(
                "This Discord account is already linked to a different Interspace user. "
                "Run `/discord_unlink` first, or unlink from the other Interspace account.",
                ephemeral=True,
            )
            return
        await ctx.respond(
            f"Link failed (status {status}). Please try again.",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        """Push role changes for any member to Interspace.

        Interspace treats this call as authoritative for the tracked-role set
        (Creator/Overseer/OIT/Administrator/Artist/Format Council). Unlinked
        users are silently no-op'd server-side, so we don't need to gate
        locally — that also means the very first sync after a link is covered
        even if linking happened on a different bot session.
        """
        if before.bot or after.bot:
            return
        before_tracked, before_active = _collect_member_roles(before)
        after_tracked, after_active = _collect_member_roles(after)
        if (
            sorted(before_tracked) == sorted(after_tracked)
            and before_active == after_active
        ):
            return
        await _interspace_post(
            "/api/discord/sync-roles",
            {
                "discordId": str(after.id),
                "roles": after_tracked,
                "isActive": after_active,
            },
        )
        # Mirror @active removal explicitly so Interspace can drop the user
        # from poll quorum immediately rather than waiting for the next pulse.
        if before_active and not after_active:
            await _interspace_post(
                "/api/discord/active-role-removed",
                {"discordId": str(after.id)},
            )

    @commands.slash_command(
        name="discord_unlink",
        description="Disconnect your Discord account from its linked Interspace account.",
    )
    async def discord_unlink(self, ctx: discord.ApplicationContext) -> None:
        if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
            await ctx.respond(
                "Interspace integration is not configured on this bot.",
                ephemeral=True,
            )
            return
        if not ctx.author:
            await ctx.respond("Cannot resolve your Discord identity.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        status, body = await _interspace_post(
            "/api/discord/link/unlink-by-discord-id",
            {"discordId": str(ctx.author.id)},
        )

        if status == 0:
            await ctx.respond(
                "Could not reach Interspace. Try again in a minute.",
                ephemeral=True,
            )
            return
        if status == 200 and body and body.get("ok"):
            if body.get("masterID"):
                await ctx.respond(
                    f"Unlinked from Interspace user **{body['masterID']}**.",
                    ephemeral=True,
                )
            else:
                await ctx.respond(
                    "Your Discord account isn't currently linked to any Interspace user.",
                    ephemeral=True,
                )
            return
        await ctx.respond(
            f"Unlink failed (status {status}). Please try again.",
            ephemeral=True,
        )
