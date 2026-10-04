"""web_search and web_fetch: ollama.com's web API, the network replaced."""

import json

import httpx
import pytest
from conftest import FakeBackend, call, say

from clara.agent import Agent, ChatRequest
from clara.prompt import SystemPrompt
from clara.tools import default_toolbox
from clara.web import PAGE_CHARS, WebClient


def transport(answers: dict, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization"), json.loads(request.content)))
        status, body = answers[request.url.path]
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


async def test_search_gives_numbered_results_with_their_urls():
    seen = []
    results = {"results": [{"title": "Python 3.14", "url": "https://python.org", "content": "Released\n  in October."}]}
    web = WebClient("key", transport=transport({"/api/web_search": (200, results)}, seen))

    text = await web.search("  python   release ", max_results=50)

    assert text.startswith("1. Python 3.14\nhttps://python.org\nReleased in October.")
    assert seen == [("/api/web_search", "Bearer key", {"query": "python release", "max_results": 10})]


async def test_fetch_cuts_long_pages_and_lists_links():
    page = {"title": "Doc", "content": "x" * (PAGE_CHARS + 50), "links": ["https://a", "https://b"]}
    web = WebClient("key", transport=transport({"/api/web_fetch": (200, page)}, []))

    text = await web.fetch("https://example.org")

    assert text.startswith("# Doc\nhttps://example.org\n\n") and "x" * PAGE_CHARS + " […]" in text
    assert text.endswith("Links:\n- https://a\n- https://b")


async def test_a_refused_key_and_a_bad_url_are_reported_to_the_model(memory, tmp_path):
    web = WebClient("bad", transport=transport({"/api/web_search": (401, {})}, []))
    backend = FakeBackend(call("web_search", query="x"), call("web_fetch", url="file:///etc/passwd"), say("sorry"))
    agent = Agent(memory, backend, default_toolbox(web), SystemPrompt(tmp_path / "none.md"))

    events = [e async for e in agent.turn(ChatRequest(surface="cli", user_id="erwan", user_name="E", message="hi"))]

    results = [e["result"] for e in events if e["type"] == "tool"]
    assert results == ["Error: the web API refused the API key", "Error: The URL must start with http:// or https://."]


def test_web_tools_are_offered_only_with_a_client():
    assert {"web_search", "web_fetch"} <= default_toolbox(WebClient("key")).names
    assert not {"web_search", "web_fetch"} & default_toolbox().names


def test_an_async_tool_is_not_run_synchronously():
    toolbox = default_toolbox(WebClient("key"))
    assert toolbox.run("web_fetch", None, {"url": "https://x"}).startswith("Error: web_fetch must be awaited")


@pytest.mark.parametrize("value, expected", [("", True), ("false", False)])
def test_the_setting_turns_them_off(value, expected):
    from clara.settings import Settings

    settings = Settings.from_env({"CLARA_TOKENS": "terminal:" + "t" * 40, "CLARA_WEB_TOOLS": value})
    assert settings.web_tools is expected
