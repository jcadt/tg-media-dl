"""Telethon client management — connect, browse channels, search, download."""

import asyncio
import os
import re
from pathlib import Path
from typing import Callable

from telethon import TelegramClient
from telethon.tl.types import (Message, InputMessagesFilterDocument,
                                DocumentAttributeVideo)

from .config import settings
from .db import save_session, load_session

SESSION_FILE = settings.DATA_DIR / "tg_series.session"


RES_RE = re.compile(r'(\d{3,4})\s*[pP]|\b4[Kk]\b|\bHD\b|\bFHD\b|\bUHD\b')


def _detect_resolution(msg: Message, fname: str) -> str:
    """Extract resolution label from video attributes or filename."""
    # Try Telegram video attributes first (most accurate)
    if msg.media and hasattr(msg.media, 'document'):
        for attr in msg.media.document.attributes:
            if hasattr(attr, 'w') and hasattr(attr, 'h'):
                w, h = attr.w, attr.h
                if w >= 3840 or h >= 2160:
                    label = "4K"
                elif w >= 1920 or h >= 1080:
                    label = "1080p"
                elif w >= 1280 or h >= 720:
                    label = "720p"
                elif w >= 854 or h >= 480:
                    label = "480p"
                else:
                    label = f"{h}p"
                return label

    # Fallback: detect from filename
    fname_lower = fname.lower()
    if '4k' in fname_lower or '2160p' in fname_lower or 'uhd' in fname_lower:
        return "4K"
    m = RES_RE.search(fname)
    if m:
        val = m.group(0).upper()
        if val == '4K':
            return '4K'
        if val in ('HD',):
            return 'HD'
        if val in ('FHD',):
            return '1080p'
        if val in ('UHD',):
            return '4K'
        # Matched digits like 720, 1080, 480
        digits = re.search(r'(\d{3,4})', val)
        if digits:
            return f"{digits.group(1)}p"

    # Check size for rough estimate
    size = msg.file.size if msg.file else 0
    if size > 2_000_000_000:  # >2GB likely 1080p+
        return "≈1080p+"
    if size > 800_000_000:    # >800MB likely 720p+
        return "≈720p+"

    return "?"


def _api_ready() -> bool:
    return settings.TG_API_ID != 0 and bool(settings.TG_API_HASH)


async def get_client(phone: str | None = None) -> TelegramClient:
    """Return an authenticated client. First call requires phone + code."""
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Try loading stored session string first
    stored = load_session("default")
    client = TelegramClient(str(SESSION_FILE), settings.TG_API_ID, settings.TG_API_HASH)

    if stored:
        # We use file-based session which Telethon handles natively
        pass

    await client.start(phone=phone)
    me = await client.get_me()
    # Save session string for web UI reconnection
    save_session("default", str(client.session.save()))
    return client


async def get_dialogs(client: TelegramClient) -> list[dict]:
    """List available channels/groups the user is in."""
    dialogs = await client.get_dialogs()
    result = []
    for d in dialogs:
        if d.is_group or d.is_channel:
            result.append({
                "id": d.entity.id,
                "name": d.name or "?",
                "title": d.entity.title if hasattr(d.entity, 'title') else d.name,
                "username": getattr(d.entity, 'username', None),
                "kind": "channel" if d.is_channel else "group",
                "date": d.date.isoformat() if d.date else None,
            })
    return result


async def search_media(client: TelegramClient, channel_id: int,
                       query: str | None = None,
                       progress_cb: Callable | None = None) -> list[dict]:
    """Search for video files in a channel, return metadata list."""
    entity = await client.get_entity(channel_id)
    messages: list[Message] = []

    kwargs = {"wait_time": 0.3}
    if query:
        kwargs["search"] = query

    async for msg in client.iter_messages(entity, **kwargs):
        if msg.media and hasattr(msg.media, 'document'):
            messages.append(msg)
        elif msg.file and msg.file.mime_type and msg.file.mime_type.startswith("video/"):
            messages.append(msg)

    messages.reverse()  # oldest first

    result = []
    for i, msg in enumerate(messages):
        fname = msg.file.name if msg.file and msg.file.name else f"video_{msg.id}.mp4"
        result.append({
            "id": msg.id,
            "date": msg.date.isoformat(),
            "file_name": fname,
            "file_size": msg.file.size if msg.file else 0,
            "mime_type": msg.file.mime_type if msg.file else None,
            "message": msg.message or "",
            "resolution": _detect_resolution(msg, fname),
        })
        if progress_cb:
            progress_cb(i + 1, len(messages))

    return result


async def download_media(client: TelegramClient, channel_id: int,
                         msg_id: int, dest_dir: Path,
                         progress_cb: Callable | None = None) -> Path:
    """Download a single message's media to dest_dir, return path."""
    entity = await client.get_entity(channel_id)
    msg = await client.get_messages(entity, ids=msg_id)
    if not msg or not msg.media:
        raise ValueError(f"Message {msg_id} has no downloadable media")

    dest_dir.mkdir(parents=True, exist_ok=True)

    async def callback(current, total):
        if progress_cb:
            progress_cb(current, total)

    path = await client.download_media(msg, file=str(dest_dir), progress_callback=callback)
    return Path(path)
