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

Recovery (requires Interspace backend with matching routes + ``BOT_SECRET``):
  * ``/recover_masterid`` → ``POST /api/discord/recover-master-id`` — ephemeral reply with Master ID.
  * ``/recover_password`` → ``POST /api/discord/recover-password`` — ephemeral temp password (+ Master ID).
"""

import asyncio

import aiohttp
import discord
from discord import Option
from discord.ext import commands, tasks

from config import INTERSPACE_URL, INTERSPACE_BOT_SECRET

# Server's DISCORD_ROLE_MAP normalises by case-folded key, so the exact
# casing here doesn't have to match Discord — lookup is case-insensitive.
INTERSPACE_TRACKED_ROLES_LC = {
    "creator",
    "overseer",
    "overseer in training",
    "administrator",
    "artist",
    "the format council",
}
ACTIVE_ROLE_NAME = "active"


def _discord_username_for_payload(user: discord.Member | discord.User) -> str:
    """Human-readable Discord name for Interspace profile sync (matches /discord_link convention)."""
    name = getattr(user, "global_name", None) or getattr(user, "name", None) or str(user)
    if getattr(user, "discriminator", "0") not in ("0", "", None):
        return f"{name}#{user.discriminator}"
    return str(name)


def _collect_member_roles(member: discord.Member | None) -> tuple[list[str], bool]:
    """Return (role_names_for_interspace, has_active_role) for a guild member."""
    if not member:
        return [], False
    role_names = [r.name for r in member.roles]
    # Case-insensitive match — Discord role names like "Overseer In Training"
    # vs "Overseer in Training" must both be treated as the same role.
    tracked = [r for r in role_names if r.lower() in INTERSPACE_TRACKED_ROLES_LC]
    is_active = ACTIVE_ROLE_NAME in (n.lower() for n in role_names)
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

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Start the live-sync loop once the bot is connected and has its
        guild member cache populated."""
        if not self.live_role_sync.is_running():
            self.live_role_sync.start()

    @tasks.loop(minutes=1)
    async def live_role_sync(self) -> None:
        """Continuously mirror every guild member's Discord roles to
        Interspace. on_member_update only fires on *changes*, which leaves
        stale state any time the bot misses an event (offline, restart, or
        the link being created before this listener shipped). Polling every
        minute keeps Interspace's view of Discord roles converged within
        ~one minute regardless of what events the bot did or didn't see.

        Unlinked users are silently no-op'd by the Interspace endpoint so
        we don't gate the loop on link state — that also means linking
        is reflected within one tick of the loop without any extra plumbing.
        @tasks.loop never overlaps iterations, so an unusually large guild
        that doesn't finish in 60 s simply throttles itself naturally.
        """
        for guild in self.bot.guilds:
            for member in guild.members:
                if member.bot:
                    continue
                tracked, is_active = _collect_member_roles(member)
                status, body = await _interspace_post(
                    "/api/discord/sync-roles",
                    {
                        "discordId": str(member.id),
                        "roles": tracked,
                        "isActive": is_active,
                        "discordUsername": _discord_username_for_payload(member),
                    },
                )
                if status == 0:
                    print(f"[live_role_sync] Interspace unreachable for member {member.id}")
                elif status not in (200, 204):
                    print(f"[live_role_sync] Unexpected status {status} for member {member.id}: {body}")
                # 10 req/s ceiling — a 1000-member guild finishes in <2 min.
                await asyncio.sleep(0.1)

    @live_role_sync.before_loop
    async def before_live_role_sync(self) -> None:
        await self.bot.wait_until_ready()

    @commands.slash_command(
        name="discord_resync",
        description="Admin: re-push every guild member's roles to Interspace.",
        default_member_permissions=discord.Permissions(administrator=True),
    )
    async def discord_resync(self, ctx: discord.ApplicationContext) -> None:
        """Manual re-trigger of the on_ready backfill.

        Useful if the bot was already running when an admin granted roles
        in Discord and they want Interspace to reflect them right now
        rather than wait for whatever event the role-update produces.
        """
        if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
            await ctx.respond("Interspace integration not configured.", ephemeral=True)
            return
        if not ctx.guild:
            await ctx.respond("Run this in a server.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        count = 0
        for member in ctx.guild.members:
            if member.bot:
                continue
            tracked, is_active = _collect_member_roles(member)
            await _interspace_post(
                "/api/discord/sync-roles",
                {
                    "discordId": str(member.id),
                    "roles": tracked,
                    "isActive": is_active,
                    "discordUsername": _discord_username_for_payload(member),
                },
            )
            count += 1
            await asyncio.sleep(0.1)
        await ctx.respond(f"Re-pushed roles for {count} members.", ephemeral=True)

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
        if len(clean_code) != 8:
            await ctx.respond("Please provide the link code from Interspace.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        discord_username = _discord_username_for_payload(ctx.author)

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
            mid = body.get("masterID") or "—"
            uname = body.get("username") or ""
            label = uname if uname else mid
            text = (
                f"Linked to Interspace account **{label}**.\n\n"
                f"Your Interspace **Master ID** — save this (sign-in and verification use it):\n"
                f"```{mid}```\n\n"
                "If your Discord roles updated your structural ID, you also get a confirmation in "
                "your Interspace notifications.\n"
                "Your activity on Discord and Interspace stays in sync for **@active**."
            )
            await ctx.respond(text, ephemeral=True)
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
                "discordUsername": _discord_username_for_payload(after),
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
        name="sync_my_roles",
        description="Re-push your current Discord roles to Interspace. Use this if your Overseer/OIT tab is missing.",
    )
    async def sync_my_roles(self, ctx: discord.ApplicationContext) -> None:
        """Force-sync the invoking member's Discord roles to Interspace right now.

        Covers the gap between on_member_update events: if the bot was offline
        when your role was granted, or the periodic sync hasn't run yet, this
        command gives you a self-service way to fix it without waiting for an
        admin to run /discord_resync.
        """
        if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
            await ctx.respond("Interspace integration not configured.", ephemeral=True)
            return
        if not ctx.author:
            await ctx.respond("Cannot resolve your Discord identity.", ephemeral=True)
            return

        member = ctx.guild.get_member(ctx.author.id) if ctx.guild else None
        if not member:
            await ctx.respond(
                "Could not find you as a guild member. Run this command inside the server.",
                ephemeral=True,
            )
            return

        await ctx.defer(ephemeral=True)
        tracked, is_active = _collect_member_roles(member)
        status, body = await _interspace_post(
            "/api/discord/sync-roles",
            {
                "discordId": str(ctx.author.id),
                "roles": tracked,
                "isActive": is_active,
                "discordUsername": _discord_username_for_payload(member),
            },
        )

        if status == 0:
            await ctx.respond(
                "Could not reach Interspace. Try again in a minute.",
                ephemeral=True,
            )
            return

        if status == 200 and body:
            if not body.get("linked"):
                await ctx.respond(
                    "Your Discord account is not linked to an Interspace account yet. "
                    "Go to **Interspace → Settings → Discord Link**, generate a code, "
                    "then run `/discord_link <code>` here.",
                    ephemeral=True,
                )
                return
            role_list = ", ".join(tracked) if tracked else "none"
            await ctx.respond(
                f"Roles synced to Interspace: **{role_list}**.\n"
                "If your tab is still missing, reload the Interspace page — "
                "the change may take up to 60 seconds to appear.",
                ephemeral=True,
            )
            return

        await ctx.respond(f"Sync failed (status {status}). Please try again.", ephemeral=True)

    @commands.slash_command(
        name="recover_masterid",
        description="Show your linked Interspace Master ID (private — only you see this).",
    )
    async def recover_masterid(self, ctx: discord.ApplicationContext) -> None:
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
            "/api/discord/recover-master-id",
            {"discordId": str(ctx.author.id)},
        )
        if status == 0:
            await ctx.respond(
                "Could not reach Interspace. Try again in a minute.",
                ephemeral=True,
            )
            return
        if status == 429:
            msg = (body or {}).get("error", "Too many attempts — wait a moment and try again.")
            await ctx.respond(msg, ephemeral=True)
            return
        if status == 200 and body:
            if not body.get("ok"):
                await ctx.respond(
                    body.get("hint")
                    or "No linked Interspace account found. Link in Interspace (Settings → Account), "
                    "then generate a `/discord_link` code here.",
                    ephemeral=True,
                )
                return
            mid = body.get("masterID", "")
            await ctx.respond(
                "Your Interspace **Master ID**:\n"
                f"```{mid}```\n\n"
                f"{body.get('hint') or ''}".strip(),
                ephemeral=True,
            )
            return
        await ctx.respond(
            "Recovery failed — try again or contact staff.",
            ephemeral=True,
        )

    @commands.slash_command(
        name="recover_password",
        description="Get a temporary Interspace login password (private — change it immediately on the site).",
    )
    async def recover_password(self, ctx: discord.ApplicationContext) -> None:
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
            "/api/discord/recover-password",
            {"discordId": str(ctx.author.id)},
        )
        if status == 0:
            await ctx.respond(
                "Could not reach Interspace. Try again in a minute.",
                ephemeral=True,
            )
            return
        if status == 429:
            msg = (body or {}).get("error", "Too many resets — wait a couple of minutes.")
            await ctx.respond(msg, ephemeral=True)
            return
        if status == 200 and body:
            if not body.get("ok"):
                err = (
                    body.get("error")
                    or body.get("hint")
                    or "This account can't reset via Discord."
                )
                await ctx.respond(err, ephemeral=True)
                return
            mid = body.get("masterID", "")
            pwd = body.get("tempPassword", "")
            if not pwd:
                await ctx.respond(
                    "Interspace returned an empty password — contact staff.",
                    ephemeral=True,
                )
                return
            await ctx.respond(
                "**Temporary login password** (sign in once, then change it in Interspace → Settings → Account):\n"
                f"```{pwd}```\n\n"
                f"Master ID:\n```{mid}```\n\n"
                f"{body.get('hint') or ''}".strip(),
                ephemeral=True,
            )
            return
        await ctx.respond(
            "Recovery failed — try again or contact staff.",
            ephemeral=True,
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
