"""Tailscale: the server publishes itself with `tailscale serve` (your tailnet) or `tailscale funnel` (the internet).

Clara keeps listening on 127.0.0.1; tailscaled terminates HTTPS and proxies to it. This module only drives the
`tailscale` command line: it learns the machine's name, sets the mapping at startup and removes it when the server
exits. It never raises: whatever goes wrong becomes `problem`, shown in the log and by `/status`, and the
server keeps running on localhost.

The HTTPS port (443, 8443 or 10000: the only ones Tailscale allows) belongs to Clara while this is on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

MODES = ("off", "serve", "funnel")
HTTPS_PORTS = (443, 8443, 10000)  # what `tailscale serve` / `funnel` can listen on
COMMAND_TIMEOUT = 20.0  # seconds; `funnel` waits for approval when it is not allowed yet


@dataclass(frozen=True)
class CommandOutput:
    returncode: int | None  # None: it did not finish in time and was killed
    text: str  # stdout and stderr


Runner = Callable[[list[str], float], Awaitable[CommandOutput]]


async def run_command(args: list[str], timeout: float) -> CommandOutput:
    """Run a command with no input, kill it after `timeout`. Raises FileNotFoundError if it does not exist."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        try:
            out, _ = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            out, _ = await process.communicate()
            return CommandOutput(None, out.decode(errors="replace").strip())
        return CommandOutput(process.returncode, out.decode(errors="replace").strip())
    finally:
        if process.returncode is None:  # cancelled: do not leave it behind
            process.kill()


class Tailscale:
    def __init__(
        self,
        mode: str,
        https_port: int,
        target: str,
        binary: str = "tailscale",
        runner: Runner = run_command,
    ):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        self.mode = mode
        self.https_port = https_port
        self.target = target  # where tailscaled forwards to, e.g. "http://127.0.0.1:8765"
        self.binary = binary
        self._run = runner
        self.url: str | None = None  # set once the mapping is in place
        self.problem: str | None = None
        self._touched = False  # we asked tailscaled for a mapping, so we remove it on exit

    @classmethod
    def from_settings(cls, settings, runner: Runner = run_command) -> Tailscale:
        host = "127.0.0.1" if settings.host in ("0.0.0.0", "::", "") else settings.host
        if ":" in host:
            host = f"[{host}]"
        return cls(
            settings.tailscale,
            settings.tailscale_port,
            f"http://{host}:{settings.port}",
            settings.tailscale_bin,
            runner,
        )

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def public(self) -> bool:
        return self.mode == "funnel"

    def describe(self) -> str:
        """One line for /status."""
        if not self.enabled:
            return "off"
        if self.url:
            return f"{self.mode}: {self.url}" + (" (public internet)" if self.public else " (your tailnet only)")
        if self.problem:
            return f"{self.mode}: NOT AVAILABLE, {self.problem}"
        return f"{self.mode}: starting..."

    async def start(self) -> None:
        if not self.enabled:
            return
        try:
            host = await self._machine_name()
            if self.mode == "serve":  # a mapping left by an earlier `funnel` would stay public
                await self._execute(["funnel", f"--https={self.https_port}", "off"], must_succeed=False)
            self._touched = True
            await self._execute([self.mode, "--bg", f"--https={self.https_port}", self.target])
        except _Problem as problem:
            self.problem = str(problem)
            log.warning("Tailscale %s is not available: %s", self.mode, problem)
            return
        self.url = f"https://{host}" + ("" if self.https_port == 443 else f":{self.https_port}")
        log.info(
            "Tailscale %s: %s -> %s (%s)", self.mode, self.url, self.target,
            "open to the whole internet, protected by the tokens only" if self.public else "your tailnet only",
        )

    async def check(self) -> str | None:
        """None if `start` would find tailscaled running (nothing is changed), else what is wrong."""
        if not self.enabled:
            return None
        try:
            await self._machine_name()
        except _Problem as problem:
            return str(problem)
        return None

    async def stop(self) -> None:
        """Remove the mapping set by `start`."""
        if not self._touched:
            return
        self._touched = False
        self.url = None
        try:
            await self._execute([self.mode, f"--https={self.https_port}", "off"])
        except _Problem as problem:
            log.warning("Could not remove the Tailscale %s mapping (run `tailscale %s --https=%d off`): %s",
                        self.mode, self.mode, self.https_port, problem)
        else:
            log.info("Tailscale %s removed", self.mode)

    async def _machine_name(self) -> str:
        output = await self._execute(["status", "--json"])
        try:
            status = json.loads(output.text)
            state = status.get("BackendState")
            name = str(status["Self"]["DNSName"]).rstrip(".")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise _Problem("`tailscale status --json` did not say who this machine is") from None
        if state != "Running":
            raise _Problem(f"Tailscale is not running on this machine (state: {state}); try `sudo tailscale up`")
        if not name:
            raise _Problem("this machine has no Tailscale DNS name: enable MagicDNS and HTTPS in the admin console")
        return name

    async def _execute(self, args: list[str], must_succeed: bool = True) -> CommandOutput:
        command = [self.binary, *args]
        shown = " ".join(command)
        try:
            output = await self._run(command, COMMAND_TIMEOUT)
        except FileNotFoundError:
            if not must_succeed:
                return CommandOutput(1, "")
            raise _Problem(f"{self.binary!r} was not found: install Tailscale, or set CLARA_TAILSCALE_BIN") from None
        except OSError as error:
            if not must_succeed:
                return CommandOutput(1, "")
            raise _Problem(f"could not run {shown}: {error}") from None
        if output.returncode == 0 or not must_succeed:
            return output
        if output.returncode is None:
            raise _Problem(
                f"`{shown}` did not finish in {COMMAND_TIMEOUT:.0f} s. If it printed a link, open it to allow "
                f"{self.mode} for this machine, then restart. Output: {output.text or '(none)'}"
            )
        hint = ""
        lowered = output.text.lower()
        if "access denied" in lowered or "permission" in lowered or "operator" in lowered:
            hint = " (the user running Clara may need: sudo tailscale set --operator=$USER)"
        raise _Problem(f"`{shown}` failed ({output.returncode}): {output.text or '(no output)'}{hint}")


class _Problem(Exception):
    """Something Tailscale-side went wrong; the text is meant for the operator."""
