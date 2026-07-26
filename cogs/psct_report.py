"""PSCT lint feedback from #report-a-problem → admin approve → Cursor cloud agent → PR."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import aiohttp
import discord
from discord import Option
from discord.ext import commands

import database as db
from config import REPORT_CHANNEL_ID, REPORT_CHANNEL_NAME
from cursor_psct import (
    create_psct_fix_agent,
    cursor_configured,
    resume_psct_fix_agent,
    wait_for_agent_run,
)

STATUS_COLORS = {
    "pending": 0xF0B429,
    "rejected": 0x9CA3AF,
    "running": 0x3B82F6,
    "pr_opened": 0x22C55E,
    "failed": 0xEF4444,
}

STATUS_LABELS = {
    "pending": "Pending admin review",
    "rejected": "Rejected",
    "running": "Cursor agent running…",
    "pr_opened": "PR opened — review & merge",
    "failed": "Agent failed",
}


def _is_admin(member: discord.Member | discord.User | None) -> bool:
    if member is None or not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    return bool(perms.administrator or perms.manage_guild or perms.manage_messages)


def _allowed_report_channel(channel: discord.abc.GuildChannel | None) -> bool:
    if channel is None:
        return False
    if REPORT_CHANNEL_ID is not None:
        return channel.id == REPORT_CHANNEL_ID
    name = (getattr(channel, "name", None) or "").lower()
    target = REPORT_CHANNEL_NAME.lower().lstrip("#")
    return name == target or name.endswith(target)


def _clip(text: str, limit: int = 900) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def build_report_embed(report: dict) -> discord.Embed:
    status = report.get("status") or "pending"
    embed = discord.Embed(
        title="PSCT lint report",
        color=STATUS_COLORS.get(status, 0x5865F2),
        description=f"**Status:** {STATUS_LABELS.get(status, status)}",
    )
    embed.add_field(name="Card type", value=_clip(str(report.get("card_type") or "—"), 100), inline=True)
    reporter = report.get("reporter_tag") or report.get("reporter_id")
    embed.add_field(name="Reporter", value=str(reporter), inline=True)
    embed.add_field(name="Card text", value=f"```\n{_clip(str(report.get('card_text') or ''), 900)}\n```", inline=False)
    embed.add_field(name="Problem", value=_clip(str(report.get("problem") or "")), inline=False)
    embed.add_field(name="Expected", value=_clip(str(report.get("expected") or "")), inline=False)
    extra = (report.get("extra") or "").strip()
    if extra:
        embed.add_field(name="Extra", value=_clip(extra, 400), inline=False)
    if report.get("pr_url"):
        embed.add_field(name="Pull request", value=str(report["pr_url"]), inline=False)
    if report.get("cursor_agent_url"):
        embed.add_field(name="Cursor agent", value=str(report["cursor_agent_url"]), inline=False)
    if report.get("error"):
        embed.add_field(name="Error", value=_clip(str(report["error"]), 500), inline=False)
    if report.get("reject_reason"):
        embed.add_field(name="Reject reason", value=_clip(str(report["reject_reason"]), 400), inline=False)
    embed.set_footer(text=f"Report ID: {report.get('id')}")
    return embed


class PsctReportView(discord.ui.View):
    """Persistent Approve / Reject buttons keyed by report id in custom_id."""

    def __init__(self, report_id: str, *, show_retry: bool = False):
        super().__init__(timeout=None)
        self.report_id = report_id

        approve = discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"psct:approve:{report_id}",
        )
        reject = discord.ui.Button(
            label="Reject",
            style=discord.ButtonStyle.danger,
            custom_id=f"psct:reject:{report_id}",
        )
        approve.callback = self._on_approve  # type: ignore[method-assign]
        reject.callback = self._on_reject  # type: ignore[method-assign]
        self.add_item(approve)
        self.add_item(reject)

        if show_retry:
            retry = discord.ui.Button(
                label="Retry agent",
                style=discord.ButtonStyle.primary,
                custom_id=f"psct:retry:{report_id}",
            )
            retry.callback = self._on_approve  # type: ignore[method-assign]
            self.add_item(retry)

    async def _on_approve(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message(
                "Only admins/mods can approve PSCT reports.", ephemeral=True
            )
            return
        report = await db.get_psct_report(self.report_id)
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return
        if report["status"] in ("running", "pr_opened"):
            await interaction.response.send_message(
                f"Already `{report['status']}`.", ephemeral=True
            )
            return
        if not cursor_configured():
            await interaction.response.send_message(
                "CURSOR_API_KEY is not configured on the bot host.", ephemeral=True
            )
            return

        await interaction.response.defer()
        # Retry after failure should launch a fresh agent, not re-poll the old one.
        clear_ids = report["status"] == "failed"
        updated = await db.update_psct_report(
            self.report_id,
            status="running",
            approver_id=interaction.user.id if interaction.user else None,
            error=None,
            **(
                {
                    "cursor_agent_id": None,
                    "cursor_run_id": None,
                    "cursor_agent_url": None,
                    "pr_url": None,
                }
                if clear_ids
                else {}
            ),
        )
        if updated and interaction.message:
            await interaction.message.edit(embed=build_report_embed(updated), view=None)

        cog = interaction.client.get_cog("PsctReportCog")
        if cog and isinstance(cog, PsctReportCog):
            cog.spawn_agent_task(self.report_id, interaction.message)

    async def _on_reject(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message(
                "Only admins/mods can reject PSCT reports.", ephemeral=True
            )
            return
        report = await db.get_psct_report(self.report_id)
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return
        if report["status"] not in ("pending", "failed"):
            await interaction.response.send_message(
                f"Cannot reject a report in status `{report['status']}`.", ephemeral=True
            )
            return
        await interaction.response.send_modal(RejectReasonModal(self.report_id))


class RejectReasonModal(discord.ui.Modal):
    def __init__(self, report_id: str):
        super().__init__(title="Reject PSCT report")
        self.report_id = report_id
        self.reason = discord.ui.InputText(
            label="Reason (optional)",
            style=discord.InputTextStyle.long,
            required=False,
            max_length=500,
        )
        self.add_item(self.reason)

    async def callback(self, interaction: discord.Interaction) -> None:
        reason = (self.reason.value or "").strip() or None
        updated = await db.update_psct_report(
            self.report_id,
            status="rejected",
            reject_reason=reason,
            approver_id=interaction.user.id if interaction.user else None,
        )
        if updated and interaction.message:
            await interaction.message.edit(embed=build_report_embed(updated), view=None)
        elif updated and interaction.channel and updated.get("message_id"):
            try:
                msg = await interaction.channel.fetch_message(int(updated["message_id"]))
                await msg.edit(embed=build_report_embed(updated), view=None)
            except Exception:
                pass
        await interaction.response.send_message("Report rejected.", ephemeral=True)


class PsctReportModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Report a PSCT lint problem")
        self.card_type = discord.ui.InputText(
            label="Card type",
            placeholder="e.g. Trap/Normal, Spell/Equip, Monster",
            max_length=100,
            required=True,
        )
        self.card_text = discord.ui.InputText(
            label="Card text (exact)",
            style=discord.InputTextStyle.long,
            max_length=1800,
            required=True,
        )
        self.problem = discord.ui.InputText(
            label="What did the linter get wrong?",
            style=discord.InputTextStyle.long,
            max_length=800,
            required=True,
        )
        self.expected = discord.ui.InputText(
            label="What should happen instead?",
            style=discord.InputTextStyle.long,
            max_length=800,
            required=True,
        )
        self.extra = discord.ui.InputText(
            label="Extra (card name, links) — optional",
            style=discord.InputTextStyle.short,
            max_length=200,
            required=False,
        )
        for item in (self.card_type, self.card_text, self.problem, self.expected, self.extra):
            self.add_item(item)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not interaction.channel:
            await interaction.response.send_message("Must be used in a server channel.", ephemeral=True)
            return
        if not _allowed_report_channel(interaction.channel):  # type: ignore[arg-type]
            where = f"<#{REPORT_CHANNEL_ID}>" if REPORT_CHANNEL_ID else f"#{REPORT_CHANNEL_NAME}"
            await interaction.response.send_message(
                f"Use this command in {where} only.", ephemeral=True
            )
            return

        report_id = str(uuid.uuid4())
        tag = str(interaction.user) if interaction.user else "unknown"
        report = await db.create_psct_report(
            report_id=report_id,
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            reporter_id=interaction.user.id if interaction.user else 0,
            reporter_tag=tag,
            card_text=self.card_text.value.strip(),
            problem=self.problem.value.strip(),
            expected=self.expected.value.strip(),
            card_type=self.card_type.value.strip(),
            extra=(self.extra.value or "").strip() or None,
        )

        view = PsctReportView(report_id)
        await interaction.response.send_message(
            content=f"New PSCT report from {interaction.user.mention if interaction.user else 'someone'}",
            embed=build_report_embed(report),
            view=view,
        )
        try:
            msg = await interaction.original_response()
            await db.update_psct_report(report_id, message_id=msg.id)
        except Exception:
            pass


class PsctReportCog(commands.Cog):
    """Slash command + admin gate + Cursor cloud agent for PSCT lint fixes."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn_agent_task(
        self,
        report_id: str,
        message: discord.Message | None,
    ) -> None:
        task = asyncio.create_task(self._run_agent(report_id, message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_agent(self, report_id: str, message: discord.Message | None) -> None:
        report = await db.get_psct_report(report_id)
        if not report:
            return

        try:
            if report.get("cursor_agent_id"):
                result = await resume_psct_fix_agent(report)
            else:
                created = await create_psct_fix_agent(report)
                if created.status != "running" or not created.agent_id:
                    result = created
                else:
                    # Persist ids before the long poll so a restart can resume.
                    mid = await db.update_psct_report(
                        report_id,
                        status="running",
                        cursor_agent_id=created.agent_id,
                        cursor_run_id=created.run_id,
                        cursor_agent_url=created.agent_url,
                        error=None,
                    )
                    if mid and message:
                        try:
                            await message.edit(embed=build_report_embed(mid), view=None)
                        except Exception:
                            pass
                    async with aiohttp.ClientSession() as session:
                        result = await wait_for_agent_run(
                            session,
                            agent_id=created.agent_id,
                            run_id=created.run_id,
                            agent_url=created.agent_url,
                        )
        except Exception as exc:
            updated = await db.update_psct_report(
                report_id,
                status="failed",
                error=f"Unhandled error: {exc}"[:500],
            )
        else:
            fields: dict[str, Any] = {
                "status": result.status if result.status in ("pr_opened", "failed") else "failed",
                "cursor_agent_id": result.agent_id or report.get("cursor_agent_id"),
                "cursor_run_id": result.run_id or report.get("cursor_run_id"),
                "cursor_agent_url": result.agent_url or report.get("cursor_agent_url"),
                "pr_url": result.pr_url,
                "error": result.error,
            }
            updated = await db.update_psct_report(report_id, **fields)

        if not updated:
            return

        view = (
            PsctReportView(report_id, show_retry=True)
            if updated["status"] == "failed"
            else None
        )
        embed = build_report_embed(updated)
        target = message
        if target is None and updated.get("message_id") and updated.get("channel_id"):
            channel = self.bot.get_channel(int(updated["channel_id"]))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(updated["channel_id"]))
                except Exception:
                    channel = None
            if isinstance(channel, discord.TextChannel):
                try:
                    target = await channel.fetch_message(int(updated["message_id"]))
                except Exception:
                    target = None
        if target is not None:
            try:
                await target.edit(embed=embed, view=view)
            except Exception as exc:
                print(f"[psct-report] failed to edit message for {report_id}: {exc}")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Re-attach persistent button views after restart.
        try:
            pending = await db.list_psct_reports_needing_views()
            for report in pending:
                show_retry = report.get("status") == "failed"
                self.bot.add_view(PsctReportView(report["id"], show_retry=show_retry))
            print(f"[psct-report] reattached views for {len(pending)} report(s)")
        except Exception as exc:
            print(f"[psct-report] view reattach failed: {exc}")

        # Resume polling for agents that were mid-flight when the bot restarted.
        try:
            running = await db.list_psct_reports_running()
            for report in running:
                self.spawn_agent_task(report["id"], None)
            if running:
                print(f"[psct-report] resumed {len(running)} running agent(s)")
        except Exception as exc:
            print(f"[psct-report] resume running failed: {exc}")

    @commands.slash_command(
        name="psct_report",
        description="Report a PSCT linter false positive/negative (use in #report-a-problem).",
    )
    async def psct_report(self, ctx: discord.ApplicationContext) -> None:
        if not ctx.guild or not ctx.channel:
            await ctx.respond("Must be used in a server.", ephemeral=True)
            return
        if not _allowed_report_channel(ctx.channel):  # type: ignore[arg-type]
            where = f"<#{REPORT_CHANNEL_ID}>" if REPORT_CHANNEL_ID else f"#{REPORT_CHANNEL_NAME}"
            await ctx.respond(f"Use this command in {where} only.", ephemeral=True)
            return
        await ctx.send_modal(PsctReportModal())

    @commands.slash_command(
        name="psct_report_status",
        description="Look up a PSCT report by ID (admin).",
    )
    async def psct_report_status(
        self,
        ctx: discord.ApplicationContext,
        report_id: Option(str, "Report UUID", required=True),
    ) -> None:
        if not _is_admin(ctx.author):
            await ctx.respond("Admins/mods only.", ephemeral=True)
            return
        report = await db.get_psct_report(report_id.strip())
        if not report:
            await ctx.respond("Report not found.", ephemeral=True)
            return
        view = None
        if report["status"] in ("pending", "failed"):
            view = PsctReportView(report["id"], show_retry=report["status"] == "failed")
        await ctx.respond(embed=build_report_embed(report), view=view, ephemeral=True)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(PsctReportCog(bot))
