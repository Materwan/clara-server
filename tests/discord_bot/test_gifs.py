"""The GIFs of Discord's picker: fetched only from Tenor and Giphy, and read by the server like any picture."""

import base64
from io import BytesIO

import httpx
import pytest
from PIL import Image as Raster

from clara.discord_bot import gifs
from clara.discord_bot.gifs import allowed, gif_links, gif_of
from clara.discord_bot.handler import MessageHandler

from .conftest import FakeBot, FakeChannel, FakeGuild, FakeMessage, FakeUser

TENOR_VIEW = "https://tenor.com/view/happy-cat-gif-10804346947536782797"
TENOR_GIF = "https://media1.tenor.com/m/lfDATg4Bhc0AAAAC/happy-cat.gif"
GIPHY_VIEW = "https://giphy.com/gifs/bailando-ai-dance-2banana1-tphCApwvdtC1VJabZ1"
GIPHY_GIF = "https://media.giphy.com/media/tphCApwvdtC1VJabZ1/giphy.gif"
CLARA = FakeUser(1, "clara", "Clara", bot=True)
ERWAN = FakeUser(111, "erwan", "Erwan")


def gif_bytes(frames: int = 2) -> bytes:
    images = [Raster.new("RGB", (16, 16), (200, 30 * number, 30)) for number in range(frames)]
    buffer = BytesIO()
    images[0].save(buffer, format="GIF", save_all=True, append_images=images[1:], duration=100, loop=0)
    return buffer.getvalue()


def page(image: str) -> str:
    return f'<html><head><meta class="dynamic" property="og:image" content="{image}"></head></html>'


def tenor_and_giphy(request: httpx.Request) -> httpx.Response:
    if request.url == TENOR_VIEW:
        return httpx.Response(200, text=page(TENOR_GIF))
    if request.url.host == "media1.tenor.com" or request.url == GIPHY_GIF:
        return httpx.Response(200, content=gif_bytes(), headers={"content-type": "image/gif"})
    return httpx.Response(404)


def test_the_links_of_a_message_are_found_once_and_only_the_picker_services():
    text = f"{TENOR_VIEW} {TENOR_VIEW} {GIPHY_VIEW} https://example.com/view/x https://tenor.com.evil.example/view/y"
    assert gif_links(text) == [TENOR_VIEW, GIPHY_VIEW]


def test_only_https_pages_of_those_two_services_are_fetched():
    assert allowed(TENOR_VIEW) and allowed(GIPHY_GIF) and allowed("https://media4.giphy.com/x.gif")
    assert not allowed("http://tenor.com/view/x") and not allowed("https://example.com/x.gif")
    assert not allowed("https://tenor.com.example.org/x")


async def test_a_tenor_link_gives_the_gif_its_page_names():
    data = await gif_of(TENOR_VIEW, httpx.MockTransport(tenor_and_giphy))
    assert data is not None and data[:6] == b"GIF89a"


async def test_a_giphy_link_gives_the_gif_of_its_id():
    data = await gif_of(GIPHY_VIEW, httpx.MockTransport(tenor_and_giphy))
    assert data is not None and data[:6] == b"GIF89a"


async def test_a_redirect_away_from_those_services_is_not_followed(monkeypatch):
    def leaves(request: httpx.Request) -> httpx.Response:
        if request.url.host == "media.giphy.com":
            return httpx.Response(302, headers={"location": "https://example.com/evil.gif"})
        return httpx.Response(200, content=gif_bytes(), headers={"content-type": "image/gif"})

    assert await gif_of(GIPHY_VIEW, httpx.MockTransport(leaves)) is None


async def test_a_gif_bigger_than_the_server_reads_is_not_fetched(monkeypatch):
    monkeypatch.setattr(gifs, "MAX_GIF_BYTES", 100)
    assert await gif_of(GIPHY_VIEW, httpx.MockTransport(tenor_and_giphy)) is None


async def test_a_page_that_is_not_a_gif_gives_nothing():
    def not_a_gif(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>nope</html>")

    assert await gif_of(GIPHY_VIEW, httpx.MockTransport(not_a_gif)) is None


async def test_a_link_to_another_site_is_not_even_requested():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("requested")

    assert await gif_of("https://example.com/view/x", httpx.MockTransport(refuse)) is None


@pytest.fixture
async def handler(api, accounts, server):
    server.signed_in = {"111": "erwan"}
    await accounts.refresh()
    return MessageHandler(FakeBot(CLARA), api, accounts, timezone="Europe/Paris",
                          gif_transport=httpx.MockTransport(tenor_and_giphy))


SERVER = FakeGuild(5, "Home", [CLARA, ERWAN], me=CLARA)  # a channel of a server: a message there is not a private one


def message(content: str, *, attachments=()) -> FakeMessage:
    return FakeMessage(2000, content, ERWAN, FakeChannel(10), SERVER, [], attachments=list(attachments))


async def test_a_picker_link_is_read_when_she_is_addressed_in(handler, server):
    msg = message(f"<@1> what is this? {TENOR_VIEW}")
    msg.mentions = [CLARA]  # a mention: she is addressed
    await handler.handle(msg)
    body = server.bodies("/v1/chat")[0]
    assert body["mode"] == "answer"
    (picked,) = body["attachments"]
    assert picked["name"] == "happy-cat-gif-10804346947536782797.gif" and picked["mime"] == "image/gif"
    assert base64.b64decode(picked["data"])[:6] == b"GIF89a"


async def test_a_picker_link_in_a_message_nobody_addresses_is_not_fetched(handler, server):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("fetched for a message she was not addressed in")

    handler.gif_transport = httpx.MockTransport(refuse)
    await handler.handle(message(f"anyone seen this? {TENOR_VIEW}"))
    assert "attachments" not in server.bodies("/v1/chat")[0]
