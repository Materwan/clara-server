"""The traffic log: every request that comes in or goes out, in JSON Lines files.

    data/logs/traffic-2026-10-02.jsonl     one file per day (UTC), one JSON object per line

    in   every HTTP request of a client: `request` (method, path, query, which client, the body), then
         `response` (status, body, duration). An event stream (`text/event-stream`) is logged event by event
         as it is sent (`sse`), except the pieces of an answer (`token`), which are only counted: the `done`
         event that ends a turn holds the whole answer.
    out  every call to the language model: `llm_request` (provider, host, model, the messages and tools
         sent), then `llm_response` (text, tool calls, token counts, duration, error); and the checks of a
         provider (`llm_call`: listing the models, verifying the API key).

Entries of one exchange share an `id`. Never written: the bearer tokens (only the name of the client is),
the API key (it never passes here), and the values of fields called code, token, password, api_key...
in bodies. Bodies longer than `max_body` characters are cut.

**Mind that** the log holds what people say and what Clara knows about them (the prompts carry the facts):
`/forget-person` does not erase it. Old files are deleted after `CLARA_TRAFFIC_LOG_DAYS` days, and
`CLARA_TRAFFIC_LOG=false` turns the log off.

The writing is done by a background thread: a request never waits for the disk.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .private import private_dir

log = logging.getLogger(__name__)

FILE_PREFIX = "traffic-"
SECRET_KEYS = re.compile(r"(?i)^(.*password.*|code|token|tokens|secret|api_?key|authorization)$")
REDACTED = "[redacted]"


def _private_opener(path: str, flags: int) -> int:
    """Open with the mode of a private file: what people said is for the person who runs the server only."""
    return os.open(path, flags, 0o600)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def redact(value: Any) -> Any:
    """`value` with the secrets of its dictionaries (at any depth) replaced."""
    if isinstance(value, dict):
        return {
            key: REDACTED if isinstance(key, str) and SECRET_KEYS.match(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class TrafficLog:
    def __init__(
        self,
        directory: Path,
        retention_days: int = 30,
        max_body: int = 100_000,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ):
        self.directory = directory
        self.retention_days = retention_days
        self.max_body = max_body
        self._today = today
        self._queue: queue.Queue[str | tuple[re.Pattern[str], list[int], threading.Event] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._write, name="traffic-log", daemon=True)
        self._thread.start()

    # -- what is written ------------------------------------------------------------------------- #

    def body(self, value: Any) -> Any:
        """A body as it is logged: redacted, and cut if it is too long (then it becomes a text)."""
        value = redact(value)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        if len(text) <= self.max_body:
            return value
        return f"{text[: self.max_body]}…[cut: {len(text):,} characters in all]"

    def raw_body(self, data: bytes) -> Any:
        """A body received or sent as bytes: JSON when it is JSON, else text."""
        if not data:
            return None
        text = data.decode("utf-8", errors="replace")
        try:
            return self.body(json.loads(text))
        except ValueError:
            return self.body(text)

    def record(self, entry: dict[str, Any]) -> None:
        """Queue one entry. It is turned into text at once: what it refers to may change afterwards."""
        stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
        try:
            line = json.dumps({"ts": stamp, **entry}, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as error:
            line = json.dumps({"ts": stamp, "kind": entry.get("kind"), "error": f"not loggable: {error}"})
        self._queue.put(line)

    # -- the writer thread ------------------------------------------------------------------------ #

    def path_for(self, day: date) -> Path:
        return self.directory / f"{FILE_PREFIX}{day.isoformat()}.jsonl"

    def prune(self) -> int:
        """Delete the files older than the retention. Returns how many went."""
        oldest = self._today() - timedelta(days=self.retention_days)
        removed = 0
        for path in self.directory.glob(f"{FILE_PREFIX}*.jsonl"):
            try:
                day = date.fromisoformat(path.stem[len(FILE_PREFIX):])
            except ValueError:
                continue
            if day < oldest:
                try:
                    path.unlink()
                    removed += 1
                except OSError as error:
                    log.warning("traffic log: cannot delete %s: %s", path, error)
        return removed

    def _write(self) -> None:
        handle = None
        day: date | None = None
        try:
            private_dir(self.directory)
            while True:
                line = self._queue.get()
                if line is None:
                    return
                if isinstance(line, tuple):  # an erasure, done here: nobody else writes the files meanwhile
                    if handle is not None:
                        handle.close()
                    handle, day = None, None  # reopened (and the old files pruned) at the next line
                    pattern, count, done = line
                    count[0] += self._scrub(pattern)
                    done.set()
                    continue
                today = self._today()
                if today != day:  # a new day: a new file, and the old ones may go
                    if handle is not None:
                        handle.close()
                    day = today
                    handle = open(self.path_for(day), "a", encoding="utf-8", opener=_private_opener)
                    self.prune()
                handle.write(line + "\n")
                if self._queue.empty():
                    handle.flush()
        except Exception:
            log.exception("traffic log: writing failed, the log is off until the server restarts")
        finally:
            if handle is not None:
                handle.close()

    def _scrub(self, pattern: re.Pattern[str]) -> int:
        """Rewrite every file without the lines `pattern` finds in. Returns how many went."""
        removed = 0
        for path in sorted(self.directory.glob(f"{FILE_PREFIX}*.jsonl")):
            try:
                with open(path, encoding="utf-8", errors="surrogateescape") as source:
                    lines = source.readlines()
                kept = [line for line in lines if not pattern.search(line)]
                if len(kept) == len(lines):
                    continue
                temporary = path.with_suffix(".tmp")
                with open(temporary, "w", encoding="utf-8", errors="surrogateescape", opener=_private_opener) as target:
                    target.writelines(kept)
                os.replace(temporary, path)
                removed += len(lines) - len(kept)
            except OSError as error:
                log.warning("traffic log: cannot erase from %s: %s", path, error)
        return removed

    def erase(self, pattern: re.Pattern[str], timeout: float = 60.0) -> int:
        """Delete the lines (requests, answers, prompts) that `pattern` finds: what a person said must go with them.
        Returns how many lines went."""
        count, done = [0], threading.Event()
        self._queue.put((pattern, count, done))
        done.wait(timeout)
        return count[0]

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until what was queued is written (for tests, and before stopping)."""
        deadline = time.monotonic() + timeout
        while not self._queue.empty() and self._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.02)  # the last line taken from the queue

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(5.0)

    # -- calls to the language model -------------------------------------------------------------- #

    async def model_stream(
        self, stream: AsyncIterator[Any], peer: str, host: str, model: str, messages: list[dict], tools: Any
    ) -> AsyncIterator[Any]:
        """The chunks of a model's answer, logged: the request now, the answer when it ends."""
        exchange = new_id()
        self.record(
            {
                "dir": "out", "kind": "llm_request", "id": exchange, "peer": peer, "host": host, "model": model,
                "messages": self.body(messages), "tools": self.body(tools or []),
            }
        )
        started = time.monotonic()
        text: list[str] = []
        calls: list[dict] = []
        tokens = [0, 0]
        error = None
        try:
            async for chunk in stream:
                text.append(chunk.text)
                calls.extend({"name": call.name, "arguments": call.arguments} for call in chunk.tool_calls)
                tokens[0] += chunk.prompt_tokens
                tokens[1] += chunk.completion_tokens
                yield chunk
        except GeneratorExit:
            error = "closed by the server (the client left, or the turn was stopped)"
            raise
        except BaseException as failure:
            error = f"{type(failure).__name__}: {failure}"
            raise
        finally:
            self.record(
                {
                    "dir": "out", "kind": "llm_response", "id": exchange, "peer": peer, "model": model,
                    "text": self.body("".join(text)), "tool_calls": self.body(calls),
                    "prompt_tokens": tokens[0], "completion_tokens": tokens[1],
                    "duration_ms": round(1000 * (time.monotonic() - started)), "error": error,
                }
            )

    async def model_call(self, operation: str, peer: str, host: str, call: Awaitable[Any]) -> Any:
        """Another request to the model's provider (listing models, verifying the key), logged."""
        started = time.monotonic()
        entry: dict[str, Any] = {"dir": "out", "kind": "llm_call", "id": new_id(), "peer": peer, "host": host,
                                 "operation": operation}
        try:
            result = await call
        except BaseException as failure:
            entry["error"] = f"{type(failure).__name__}: {failure}"
            raise
        else:
            entry["result"] = self.body(result)
            return result
        finally:
            entry["duration_ms"] = round(1000 * (time.monotonic() - started))
            self.record(entry)


class _SseParser:
    """Cuts the bytes of an event stream into events: `(type, data)`; keepalive comments are dropped."""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[tuple[str, Any]]:
        self._buffer += chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
        events = []
        while "\n\n" in self._buffer:
            block, self._buffer = self._buffer.split("\n\n", 1)
            kind, data = "message", []
            for line in block.split("\n"):
                if line.startswith("event:"):
                    kind = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
            if not data:
                continue  # a comment (": keepalive")
            text = "\n".join(data)
            try:
                events.append((kind, json.loads(text)))
            except ValueError:
                events.append((kind, text))
        return events


class TrafficMiddleware:
    """ASGI middleware that logs every HTTP exchange with the clients. It never buffers a response: an
    event stream goes on flowing while it is logged."""

    def __init__(self, app: Any, traffic: Callable[[], TrafficLog | None], identify: Callable[[dict[str, str]], str]):
        self.app = app
        self._traffic = traffic  # read at each request: the log is created with the application's state
        self._identify = identify  # request headers -> "client:<name>", "admin:<name>", "user:<name>@<surface>" or "anonymous"

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        traffic = self._traffic() if scope["type"] == "http" else None
        if traffic is None:
            await self.app(scope, receive, send)
            return

        exchange = new_id()
        started = time.monotonic()
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        common = {"id": exchange, "peer": self._identify(headers)}
        client = scope.get("client")
        received: list[bytes] = []
        state = {"request_logged": False, "status": None, "sse": False, "events": 0, "tokens": 0, "size": 0}
        sent: list[bytes] = []  # the start of a response body, kept to be logged (state["size"] counts all of it)
        parser = _SseParser()

        def log_request() -> None:
            if state["request_logged"]:
                return
            state["request_logged"] = True
            traffic.record(
                {
                    "dir": "in", "kind": "request", **common, "method": scope["method"], "path": scope["path"],
                    "query": scope.get("query_string", b"").decode("latin-1"),
                    "from": (f"{client[0]}:{client[1]}" if client[1] else client[0]) if client else None,  # port 0: forwarded
                    "body": traffic.raw_body(b"".join(received)),
                }
            )

        async def logged_receive() -> dict:
            message = await receive()
            if message["type"] == "http.request":
                if sum(map(len, received)) <= 4 * traffic.max_body:  # what is kept to be logged: enough to be cut later
                    received.append(message.get("body", b""))
                if not message.get("more_body"):
                    log_request()
            return message

        async def logged_send(message: dict) -> None:
            if message["type"] == "http.response.start":
                log_request()  # a request without a body may never have been read
                state["status"] = message["status"]
                content_type = dict(message.get("headers") or []).get(b"content-type", b"")
                state["sse"] = content_type.startswith(b"text/event-stream")
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                state["size"] += len(chunk)
                if state["sse"]:
                    for kind, data in parser.feed(chunk):
                        if kind == "token":
                            state["tokens"] += 1
                            continue
                        state["events"] += 1
                        traffic.record({"dir": "out", "kind": "sse", **common, "event": kind, "data": traffic.body(data)})
                elif state["size"] - len(chunk) <= 4 * traffic.max_body:  # what was kept so far: enough to be cut later
                    sent.append(chunk)
            await send(message)

        outcome = "complete"
        try:
            await self.app(scope, logged_receive, logged_send)
        except BaseException as failure:
            outcome = f"{type(failure).__name__}" if not isinstance(failure, Exception) else f"error: {failure}"
            raise
        finally:
            log_request()
            entry = {
                "dir": "out", "kind": "response", **common, "method": scope["method"], "path": scope["path"],
                "status": state["status"], "duration_ms": round(1000 * (time.monotonic() - started)),
                "bytes": state["size"], "outcome": outcome,
            }
            if state["sse"]:
                entry["events"], entry["tokens"] = state["events"], state["tokens"]
            elif scope.get("clara_sensitive"):  # a password was handed out: the answer is not written
                entry["body"] = REDACTED
            else:
                entry["body"] = traffic.raw_body(b"".join(sent))
            traffic.record(entry)
