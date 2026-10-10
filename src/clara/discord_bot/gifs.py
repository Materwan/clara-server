"""The GIFs Discord's own picker sends: a link to Tenor or Giphy, not a file. The bot fetches the GIF behind the link, and
hands it to the server as a file, so that Clara reads it like any other picture.

Only those two services are fetched, over HTTPS, redirects included (a link that leads anywhere else is ignored).
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import httpx

from ..attachments import image_type

log = logging.getLogger(__name__)

MAX_GIF_BYTES = 30_000_000  # the most the server reads from an animation
MAX_LINKS = 2  # GIF links read from one message
TIMEOUT = 15.0
USER_AGENT = "Mozilla/5.0 (compatible; Clara)"  # some of these sites refuse a client that says nothing of itself
LINK = re.compile(r"https://(?:[\w-]+\.)?tenor\.com/view/[\w-]+|https://giphy\.com/gifs/[\w-]+")
IMAGE_META = re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"')
TENOR = re.compile(r"^(?:[\w-]+\.)?tenor\.com$")
GIPHY = re.compile(r"^(?:[\w-]+\.)?giphy\.com$")
GIPHY_ID = re.compile(r"^[A-Za-z0-9]+$")


def gif_links(text: str) -> list[str]:
    """The Tenor and Giphy links in a message, each once, at most MAX_LINKS."""
    found: list[str] = []
    for link in LINK.findall(text):
        if link not in found:
            found.append(link)
    return found[:MAX_LINKS]


def allowed(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and bool(TENOR.match(host) or GIPHY.match(host))


async def gif_of(link: str, transport: httpx.AsyncBaseTransport | None = None) -> bytes | None:
    """The GIF a link points at (its bytes), or None: not one of these services, or no GIF to be had."""
    if not allowed(link):
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport, follow_redirects=True,
                                     headers={"User-Agent": USER_AGENT}) as http:
            address = await _address_of(http, link)
            if address is None or not allowed(address):
                return None
            return await _download(http, address)
    except httpx.HTTPError as error:
        log.warning("cannot fetch the GIF of %s: %s", link, error)
        return None


async def _address_of(http: httpx.AsyncClient, link: str) -> str | None:
    """Where the GIF is: Giphy's media address is made from the link's id; Tenor's page names it."""
    if "giphy.com" in link:
        identifier = link.rstrip("/").rsplit("-", 1)[-1].rsplit("/", 1)[-1]
        return f"https://media.giphy.com/media/{identifier}/giphy.gif" if GIPHY_ID.match(identifier) else None
    page = await http.get(link)
    if page.is_error:
        return None
    found = IMAGE_META.search(page.text)
    return found.group(1) if found else None


async def _download(http: httpx.AsyncClient, address: str) -> bytes | None:
    async with http.stream("GET", address) as response:
        if response.is_error or not allowed(str(response.url)):
            return None
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_GIF_BYTES:
                return None
            chunks.append(chunk)
    data = b"".join(chunks)
    return data if image_type(data) in ("image/gif", "image/webp") else None
