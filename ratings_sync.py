"""Pull/push rated-match state so the bot DB stays synced with Interspace."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from config import INTERSPACE_BOT_SECRET, INTERSPACE_URL

log = logging.getLogger("ccg-bot.ratings-sync")


def _headers() -> dict[str, str]:
    return {"x-bot-secret": INTERSPACE_BOT_SECRET, "Content-Type": "application/json"}


async def interspace_record_match(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Canonical write path: Interspace applies Elo; bot mirrors the snapshot."""
    if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
        return None
    url = f"{INTERSPACE_URL}/api/discord/ratings/record-match"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=_headers(), timeout=30) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    log.warning("record-match failed %s: %s", resp.status, text[:300])
                    return None
                return await resp.json()
    except Exception:
        log.exception("record-match request failed")
        return None


async def fetch_pending_sync() -> list[dict[str, Any]]:
    if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
        return []
    url = f"{INTERSPACE_URL}/api/discord/ratings/pending-sync"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers(), timeout=30) as resp:
                if resp.status >= 400:
                    return []
                data = await resp.json()
                return list(data.get("items") or [])
    except Exception:
        log.exception("pending-sync failed")
        return []


async def mark_synced(match_id: str) -> bool:
    if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
        return False
    url = f"{INTERSPACE_URL}/api/discord/ratings/mark-synced"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"matchId": match_id}, headers=_headers(), timeout=20) as resp:
                return resp.status < 400
    except Exception:
        log.exception("mark-synced failed")
        return False


async def fetch_ladder() -> dict[str, Any] | None:
    if not INTERSPACE_URL or not INTERSPACE_BOT_SECRET:
        return None
    url = f"{INTERSPACE_URL}/api/discord/ratings/ladder"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers(), timeout=30) as resp:
                if resp.status >= 400:
                    return None
                return await resp.json()
    except Exception:
        log.exception("ladder fetch failed")
        return None
