"""Utilities for interacting with YouTube via ``yt_dlp``."""

from __future__ import annotations

import asyncio
import logging
from typing import List

import yt_dlp

from .schemas import Track

logger = logging.getLogger(__name__)


def _extract_search_results(query: str, limit: int) -> List[Track]:
    """Execute the ``yt_dlp`` search synchronously and format the results."""

    options = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "no_warnings": True,
        "default_search": "ytsearch",
    }

    search_term = f"ytsearch{limit}:{query}"
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(search_term, download=False)

    entries = info.get("entries", []) if info else []
    tracks: List[Track] = []

    for entry in entries:
        if not entry:
            continue
        video_id = entry.get("id")
        title = entry.get("title")
        if not video_id or not title:
            continue

        url = entry.get("original_url") or entry.get("url")
        if not url:
            url = f"https://www.youtube.com/watch?v={video_id}"

        tracks.append(
            Track(
                video_id=video_id,
                title=title,
                url=url,
                channel=entry.get("uploader") or entry.get("channel"),
                duration=entry.get("duration"),
                thumbnail=entry.get("thumbnail"),
            )
        )

    return tracks


async def search_youtube(query: str, limit: int = 8) -> List[Track]:
    """Search for YouTube videos that match ``query``.

    The heavy lifting is performed on a worker thread so the FastAPI event loop
    remains responsive.
    """

    logger.info("Searching YouTube for %r (limit=%s)", query, limit)

    try:
        return await asyncio.to_thread(_extract_search_results, query, limit)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception("YouTube search failed: %s", exc)
        raise
