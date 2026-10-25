"""Comprobación periódica de una URL con enlaces magnet para añadir fuentes nuevas."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import urllib.request

from .db import Database
from .torrents import TorrentManager, parse_magnet

log = logging.getLogger("biblioteca.feed")

MAGNET_RE = re.compile(r"magnet:\?xt=urn:bt[im]h:[^\s\"'<>]+", re.I)


def extract_magnets(text: str) -> list[str]:
    seen, result = set(), []
    for raw in MAGNET_RE.findall(text):
        magnet = html.unescape(raw)
        try:
            infohash, _ = parse_magnet(magnet)
        except ValueError:
            continue
        if infohash not in seen:
            seen.add(infohash)
            result.append(magnet)
    return result


def _download(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Biblioteca/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read(5_000_000).decode("utf-8", errors="replace")


async def check_feed(url: str, db: Database, torrents: TorrentManager) -> int:
    text = await asyncio.to_thread(_download, url)
    added = 0
    for magnet in extract_magnets(text):
        infohash, _ = parse_magnet(magnet)
        if db.feed_seen(infohash):
            continue
        db.mark_feed_seen(infohash)
        if db.source_by_hash(infohash) is None:
            torrents.add_source(magnet, origin="feed")
            added += 1
            log.info("Nueva fuente desde el feed: %s", infohash)
    return added


async def feed_loop(url: str, hours: float, db: Database, torrents: TorrentManager) -> None:
    while True:
        try:
            await check_feed(url, db, torrents)
        except Exception as exc:
            log.warning("No se pudo comprobar el feed de magnets: %s", exc)
        await asyncio.sleep(max(hours, 0.25) * 3600)
