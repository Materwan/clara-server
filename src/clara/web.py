"""Searching the web and reading pages, through ollama.com's web API (the `web_search` and `web_fetch` tools).

    POST https://ollama.com/api/web_search  {"query", "max_results"}  -> {"results": [{"title", "url", "content"}]}
    POST https://ollama.com/api/web_fetch   {"url"}                   -> {"title", "content", "links"}

Both need the Ollama API key (OLLAMA_API_KEY), the one the `cloud` provider uses. What comes back is cut to
sizes a model can take: a page is about a few thousand tokens at most.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_HOST = "https://ollama.com"
TIMEOUT = 30.0
MAX_RESULTS = 10
RESULT_CHARS = 1_500  # of each search result's content
PAGE_CHARS = 12_000  # of a fetched page
LINKS_SHOWN = 30


class WebError(Exception):
    """The web API could not be reached, or refused the request."""


def _clip(text: str, limit: int, flatten: bool = False) -> str:
    """`text` cut to `limit` characters; with `flatten`, on one line."""
    text = " ".join(text.split()) if flatten else text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " […]"


class WebClient:
    def __init__(self, api_key: str, host: str = DEFAULT_HOST, transport: httpx.AsyncBaseTransport | None = None):
        self._api_key = api_key
        self._host = host.rstrip("/")
        self._transport = transport  # tests replace the network

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as http:
                response = await http.post(f"{self._host}{path}", json=body, headers=headers)
        except httpx.HTTPError as error:
            raise WebError(f"the web API could not be reached ({type(error).__name__})") from None
        if response.status_code in (401, 403):
            raise WebError("the web API refused the API key")
        if response.status_code >= 400:
            raise WebError(f"the web API answered HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise WebError("the web API sent something that is not JSON") from None
        return data if isinstance(data, dict) else {}

    async def search(self, query: str, max_results: int = 5) -> str:
        """The results as text: one block per result (title, URL, an excerpt)."""
        query = " ".join(str(query).split())
        if not query:
            raise ValueError("The query is empty.")
        count = max(1, min(int(max_results), MAX_RESULTS))
        data = await self._post("/api/web_search", {"query": query, "max_results": count})
        results = [item for item in data.get("results") or [] if isinstance(item, dict)]
        if not results:
            return f"No result for {query!r}."
        blocks = []
        for number, item in enumerate(results, start=1):
            title = str(item.get("title") or "(no title)").strip()
            blocks.append(f"{number}. {title}\n{item.get('url', '')}\n{_clip(str(item.get('content') or ''), RESULT_CHARS, flatten=True)}")
        return "\n\n".join(blocks) + "\n\nRead a page in full with web_fetch."

    async def fetch(self, url: str) -> str:
        """The page as text: its title, its content (cut), then its links."""
        url = str(url).strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("The URL must start with http:// or https://.")
        data = await self._post("/api/web_fetch", {"url": url})
        title = str(data.get("title") or "").strip()
        content = _clip(str(data.get("content") or ""), PAGE_CHARS) or "(the page has no readable text)"
        links = [str(link) for link in data.get("links") or [] if link][:LINKS_SHOWN]
        text = f"# {title}\n{url}\n\n{content}" if title else f"{url}\n\n{content}"
        if links:
            text += "\n\nLinks:\n" + "\n".join(f"- {link}" for link in links)
        return text
