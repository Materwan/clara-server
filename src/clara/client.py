"""`clara-chat`: a minimal terminal client. Also the model for writing other clients.

A client does three things: sends `surface` + `user_id` + `message`, reads the
SSE stream, shows the tokens. It owns no memory at all.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator

import httpx
from dotenv import load_dotenv

from . import session

SURFACE = "cli"

HELP = """\
/facts            what Clara remembers about you
/remember <text>  store a fact
/forget <id>      delete a fact
/linkcode         a code that lets another account attach itself to you
                  (valid 10 minutes, once)
/link <surface> <id> <code>
                  tell Clara that account (e.g. discord 1234) is you too; <code> is
                  the one that account's own client gave it with its /linkcode
/remind [daily|weekly|monthly] [@surfaces] <when> <text>
                  a notification for you, on your clients, at that time.
                  <when>: +30m, +2h, +3d | 09:30 | tomorrow 09:30 | 2026-10-05 09:30
                  @surfaces: only on those, e.g. @app or @app,discord (default: all of yours)
/reminders        your reminders that have not fired yet
/unremind <id>    cancel one of them
/notify [@surfaces] <text>
                  send yourself a notification now (e.g. to try your other clients)
/notify-after [<seconds> | off | default]
                  how long a task takes before you are notified when it is done
                  (no argument: show it; off: never; default: the server's)
/new              start a fresh conversation thread (facts are kept)
/quit             leave"""

REPEATS = ("daily", "weekly", "monthly")
LATE_SECONDS = 120  # a reminder shown this long after it fired is announced as missed
SHOWN = ("reminder", "notification", "server")  # the events of the stream this client shows


class ClaraApi:
    def __init__(self, url: str, token: str, user: str, name: str | None, conversation: str):
        self.user, self.name, self.conversation = user, name, conversation
        self.http = httpx.Client(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(10.0, read=300.0),  # a model can think for a long while
        )

    def identity(self) -> dict:
        return {"surface": SURFACE, "user_id": self.user}

    def stream_chat(self, message: str) -> Iterator[dict]:
        body = {
            **self.identity(),
            "user_name": self.name,
            "message": message,
            "conversation": self.conversation,
        }
        with self.http.stream("POST", "/v1/chat/stream", json=body) as response:
            if response.is_error:
                response.read()
                response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])

    def facts(self) -> list[dict]:
        response = self.http.get("/v1/memory/facts", params=self.identity())
        response.raise_for_status()
        return response.json()["facts"]

    def remember(self, text: str) -> bool:
        response = self.http.post(
            "/v1/memory/facts", json={**self.identity(), "user_name": self.name, "text": text}
        )
        response.raise_for_status()
        return response.json()["created"]

    def forget(self, fact_id: int) -> None:
        self.http.delete(f"/v1/memory/facts/{fact_id}", params=self.identity()).raise_for_status()

    def link_code(self) -> str:
        response = self.http.post("/v1/accounts/link-code", json=self.identity())
        response.raise_for_status()
        return response.json()["code"]

    def link(self, surface: str, external_id: str, code: str) -> list[str]:
        response = self.http.post(
            "/v1/accounts/link",
            json={
                "surface": surface,
                "user_id": external_id,
                "code": code,
                "to_surface": SURFACE,
                "to_user_id": self.user,
            },
        )
        response.raise_for_status()
        return response.json()["accounts"]

    def new_thread(self) -> None:
        self.http.delete(f"/v1/conversations/{self.conversation}").raise_for_status()

    def add_reminder(self, at: datetime, text: str, repeat: str, targets: list[str] | None = None) -> dict:
        """A reminder for this user, shown on the surfaces in `targets` (empty: on all of theirs)."""
        response = self.http.post(
            "/v1/reminders",
            json={
                **self.identity(),
                "user_name": self.name,
                "text": text,
                "repeat": repeat,
                "at": at.isoformat(timespec="seconds"),
                "targets": targets or [],
            },
        )
        response.raise_for_status()
        return response.json()

    def notify(self, text: str, title: str = "", targets: list[str] | None = None) -> dict:
        """Send this user a notification now, on the surfaces in `targets` (empty: on all of theirs)."""
        response = self.http.post(
            "/v1/notifications",
            json={**self.identity(), "user_name": self.name, "text": text, "title": title, "targets": targets or []},
        )
        response.raise_for_status()
        return response.json()

    def settings(self) -> dict:
        response = self.http.get("/v1/settings", params=self.identity())
        response.raise_for_status()
        return response.json()

    def set_notify_after(self, seconds: int | None) -> dict:
        """Seconds a task takes before it notifies this user when done (0: never; None: the server's default)."""
        response = self.http.patch(
            "/v1/settings", json={**self.identity(), "user_name": self.name, "notify_after": seconds}
        )
        response.raise_for_status()
        return response.json()

    def reminders(self) -> list[dict]:
        response = self.http.get("/v1/reminders", params=self.identity())
        response.raise_for_status()
        return response.json()["reminders"]

    def cancel_reminder(self, reminder_id: int) -> None:
        self.http.delete(f"/v1/reminders/{reminder_id}", params=self.identity()).raise_for_status()

    def reminder_events(self) -> Iterator[dict]:
        """What the server announces to this user, for as long as the connection holds: `reminder` and
        `notification` events (the ones missed while away first) and `server` events (its state: running,
        stopping, stopped). It has its own connection, so a background thread can run it."""
        with (
            httpx.Client(
                base_url=str(self.http.base_url),
                headers=self.http.headers,
                timeout=httpx.Timeout(10.0, read=60.0),  # the server sends a keepalive every 15 s
            ) as http,
            http.stream("GET", "/v1/notifications/stream", params=self.identity()) as response,
        ):
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    if event.get("type") in SHOWN:
                        yield event


def _clock(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2}):(\d\d)", text)
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError(f"Not a time of day: {text!r} (use HH:MM).")
    return int(match[1]), int(match[2])


def take_targets(argument: str) -> tuple[list[str], str]:
    """`(surfaces, the rest)`: an `@app,discord` word among the first two says where to show it."""
    words = argument.split()
    for index, word in enumerate(words[:2]):
        if word.startswith("@"):
            del words[index]
            return [name for name in word[1:].lower().split(",") if name], " ".join(words)
    return [], argument


def parse_remind(argument: str, now: datetime) -> tuple[datetime, str, str]:
    """`(when, repeat, text)` from the arguments of /remind. `now` is the local time, with its offset."""
    words = argument.split()
    repeat = words.pop(0).lower() if words and words[0].lower() in REPEATS else ""
    if not words:
        raise ValueError("Usage: /remind [daily|weekly|monthly] <when> <text>   (see /help)")
    head = words[0].lower()
    relative = re.fullmatch(r"\+(\d+)([mhd])", head)
    if relative:
        unit = {"m": "minutes", "h": "hours", "d": "days"}[relative[2]]
        due, rest = now + timedelta(**{unit: int(relative[1])}), words[1:]
    elif head == "tomorrow" and len(words) > 1:
        hour, minute = _clock(words[1])
        due = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        rest = words[2:]
    elif re.fullmatch(r"\d{4}-\d\d-\d\d", head) and len(words) > 1:
        hour, minute = _clock(words[1])
        try:
            due = datetime.fromisoformat(head).replace(hour=hour, minute=minute).astimezone()
        except ValueError:
            raise ValueError(f"Not a date: {head!r}.") from None
        rest = words[2:]
    elif ":" in head:
        hour, minute = _clock(head)
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        due, rest = (due if due > now else due + timedelta(days=1)), words[1:]
    else:
        raise ValueError(f"Cannot read the time {head!r}: use +30m, 09:30, tomorrow 09:30 or 2026-10-05 09:30.")
    text = " ".join(rest)
    if not text:
        raise ValueError("A reminder needs a text.")
    return due, repeat, text


def local(moment: str) -> str:
    return datetime.fromisoformat(moment).astimezone().strftime("%Y-%m-%d %H:%M")


def parse_notify_after(argument: str) -> int | None:
    """Seconds from the argument of /notify-after: a number, `off` (0: never) or `default` (None)."""
    word = argument.strip().lower()
    if word == "default":
        return None
    if word in ("off", "never"):
        return 0
    if not word.isdigit():
        raise ValueError("Usage: /notify-after <seconds> | off | default")
    return int(word)


def describe_notify_after(settings: dict) -> str:
    def words(seconds: int) -> str:
        return "never" if seconds == 0 else f"after {seconds} s of work"

    own, default = settings["notify_after"], settings["notify_after_default"]
    if own is None:
        return f"You are notified {words(default)} when a task is done (the server's default)."
    return f"You are notified {words(own)} when a task is done (the server's default: {words(default)})."


def describe_reminder(reminder: dict) -> str:
    again = f" ({reminder['repeat']})" if reminder["repeat"] else ""
    where = f" @{','.join(reminder['targets'])}" if reminder.get("targets") else ""
    return f"[{reminder['id']}] {local(reminder['due_at'])}{again}{where}  {reminder['text']}"


def format_reminder(event: dict, now: datetime) -> str:
    missed = (now - datetime.fromisoformat(event["fired_at"])).total_seconds() > LATE_SECONDS
    late = f"  (missed, it was due {local(event['due_at'])})" if missed else ""
    return f"\a⏰ {event.get('message') or event['text']}{late}"


def format_notification(event: dict, now: datetime) -> str:
    late = (now - datetime.fromisoformat(event["sent_at"])).total_seconds() > LATE_SECONDS
    when = f"  (sent {local(event['sent_at'])})" if late else ""
    title = f"{event['title']}: " if event.get("title") else ""
    return f"\a🔔 {title}{event['text']}{when}"


def format_event(event: dict, now: datetime) -> str:
    return (format_notification if event["type"] == "notification" else format_reminder)(event, now)


class Notices:
    """Shows reminders from the listener thread, never in the middle of a reply being printed."""

    def __init__(self, prompt: str = "\nyou> "):
        self.prompt = prompt
        self._lock = threading.Lock()
        self._busy = False
        self._waiting: list[str] = []

    def show(self, event: dict) -> None:
        self.say(format_event(event, datetime.now().astimezone()))

    def say(self, text: str) -> None:
        with self._lock:
            if self._busy:
                self._waiting.append(text)
            else:
                print(f"\n{text}\n{self.prompt}", end="", flush=True)

    @contextmanager
    def busy(self) -> Iterator[None]:
        """While the block runs (a reply streaming in), reminders wait."""
        with self._lock:
            self._busy = True
        try:
            yield
        finally:
            with self._lock:
                self._busy = False
                waiting, self._waiting = self._waiting, []
            for text in waiting:
                print(f"\n{text}")


SERVER_SAYS = {
    "stopping": "Clara is stopping: she finishes what is running and takes nothing new.",
    "down": "Clara is not running.",
    "again": "Clara is running again.",
}


def listen(api: ClaraApi, notices: Notices, stop: threading.Event, pause: float = 5.0) -> None:
    """Show every reminder and notification the server announces, and say when the server stops, is gone
    or is back; when the connection drops, try again."""
    state = ""  # "running", "stopping" or "down"; "" before the first answer

    def change(new: str) -> None:
        nonlocal state
        old, state = state, new
        if new == old or (new == "running" and old == ""):
            return
        notices.say(SERVER_SAYS["again" if new == "running" else new])

    while not stop.is_set():
        try:
            for event in api.reminder_events():
                if event["type"] == "server":
                    change({"stopped": "down"}.get(event["state"], event["state"]))
                else:
                    notices.show(event)
        except (httpx.HTTPError, OSError, ValueError):
            pass  # server down or restarting: what was missed meanwhile arrives on reconnection
        change("down")
        stop.wait(pause)


def chat(api: ClaraApi, message: str) -> None:
    for event in api.stream_chat(message):
        if event["type"] == "token":
            print(event["text"], end="", flush=True)
        elif event["type"] == "tool":
            print(f"\n  [{event['name']}]", end="", flush=True)
        elif event["type"] == "error":
            print(f"\n! {event['message']}")
    print()


def command(api: ClaraApi, line: str) -> bool:
    """Run a /command. False means: leave."""
    name, _, argument = line.partition(" ")
    argument = argument.strip()
    if name in ("/quit", "/exit"):
        return False
    if name == "/facts":
        facts = api.facts()
        print("\n".join(f"[{fact['id']}] {fact['text']}" for fact in facts) or "(nothing yet)")
    elif name == "/remember" and argument:
        print("Stored." if api.remember(argument) else "Already known.")
    elif name == "/forget" and argument.isdigit():
        api.forget(int(argument))
        print("Forgotten.")
    elif name == "/linkcode":
        print(f"Code: {api.link_code()}  (valid 10 minutes, once)")
    elif name == "/link" and len(argument.split()) == 3:
        surface, external_id, code = argument.split()
        print("Accounts: " + ", ".join(api.link(surface, external_id, code)))
    elif name == "/remind":
        targets, argument = take_targets(argument)
        try:
            due, repeat, text = parse_remind(argument, datetime.now().astimezone())
        except ValueError as error:
            print(error)
            return True
        reminder = api.add_reminder(due, text, repeat, targets)
        where = ", ".join(reminder.get("targets") or []) or "all your clients"
        print(f"Reminder {reminder['id']} set for {local(reminder['due_at'])}, shown on {where}.")
    elif name == "/notify" and take_targets(argument)[1]:
        targets, text = take_targets(argument)
        sent = api.notify(text, targets=targets)
        print(f"Sent, shown on {', '.join(sent['targets']) or 'all your clients'}.")
    elif name == "/notify-after":
        if argument:
            try:
                api.set_notify_after(parse_notify_after(argument))
            except ValueError as error:
                print(error)
                return True
        print(describe_notify_after(api.settings()))
    elif name == "/reminders":
        print("\n".join(describe_reminder(r) for r in api.reminders()) or "(none)")
    elif name == "/unremind" and argument.isdigit():
        api.cancel_reminder(int(argument))
        print("Cancelled.")
    elif name == "/new":
        api.new_thread()
        print("New thread.")
    else:
        print(HELP)
    return True


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="clara-chat", description="Talk to a Clara server.")
    parser.add_argument("--url", default=os.getenv("CLARA_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.getenv("CLARA_TOKEN"), help="a client token (CLARA_TOKEN), instead of a password")
    parser.add_argument(
        "--user", default=os.getenv("CLARA_USER") or getpass.getuser().lower(),
        help="your user name (CLARA_USER): you sign in with its password, asked once (or CLARA_PASSWORD)",
    )
    parser.add_argument("--logout", action="store_true", help="forget the saved sign-in of this user and exit")
    parser.add_argument("--name", default=None, help="how Clara should call you")
    parser.add_argument("--conversation", default=None, help="thread id (default: cli:<user>)")
    parser.add_argument("message", nargs="*", help="one-shot: send this and exit")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):  # Windows consoles are not always UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if args.logout:
        print("Signed out." if session.sign_out(args.url, args.user, SURFACE) else "No saved sign-in.")
        return
    token = args.token
    if not token:  # a user with a password: the server gives a token bound to this user and surface
        try:
            token = session.obtain_token(args.url, args.user, SURFACE)
        except session.LoginError as error:
            raise SystemExit(f"Cannot sign in: {error}") from None

    api = ClaraApi(args.url, token, args.user, args.name, args.conversation or f"{SURFACE}:{args.user}")

    def handle(line: str) -> bool:
        """Process one input line; False means: leave. Server errors are shown, not fatal."""
        try:
            if line.startswith("/"):
                return command(api, line)
            chat(api, line)
        except httpx.ConnectError:
            print(f"! Cannot reach the Clara server at {args.url}.")
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 401 and not args.token:
                session.forget(args.url, args.user, SURFACE)
                print("! You were signed out. Start clara-chat again and enter your password.")
                return False
            print(f"! Server said {error.response.status_code}: {error.response.text}")
        return True

    if args.message:
        handle(" ".join(args.message))
        return
    print("Clara — /help for commands, Ctrl+D to leave.")
    notices = Notices()
    stop = threading.Event()
    threading.Thread(target=listen, args=(api, notices, stop), daemon=True, name="reminders").start()
    try:
        while True:
            try:
                line = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if line:
                with notices.busy():
                    leave = not handle(line)
                if leave:
                    return
    finally:
        stop.set()


if __name__ == "__main__":
    main()
