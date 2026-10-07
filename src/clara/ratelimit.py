"""Refuse an address that keeps sending wrong tokens, so a public URL (Tailscale Funnel) cannot be brute-forced.

Only addresses other than the machine itself are counted: a local client with a wrong token must not lock out
the others. Behind `tailscale serve/funnel` the address is the real client's one, taken by uvicorn from
`X-Forwarded-For` (the proxy is on 127.0.0.1, which uvicorn trusts), so what is seen here is not the proxy's.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable

WINDOW_SECONDS = 60.0
MAX_TRACKED = 10_000  # addresses remembered at most; an attack from many addresses must not fill the memory


def is_local(address: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(address or "")
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


class FailureLimiter:
    def __init__(self, max_failures: int, block_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.max_failures = max_failures
        self.block_seconds = block_seconds
        self._clock = clock
        self._failures: dict[str, list[float]] = {}  # address -> times of its recent failures
        self._blocked: dict[str, float] = {}  # address -> when the block ends

    @property
    def enabled(self) -> bool:
        return self.max_failures > 0

    def blocked_for(self, address: str | None) -> float:
        """Seconds this address is still refused for (0: not blocked)."""
        if not self.enabled or address is None or is_local(address):
            return 0.0
        until = self._blocked.get(address)
        if until is None:
            return 0.0
        remaining = until - self._clock()
        if remaining <= 0:
            del self._blocked[address]
            return 0.0
        return remaining

    def failed(self, address: str | None) -> bool:
        """Note a wrong token. True when this one starts a block."""
        if not self.enabled or address is None or is_local(address):
            return False
        now = self._clock()
        recent = [t for t in self._failures.get(address, []) if now - t < WINDOW_SECONDS]
        recent.append(now)
        if len(recent) >= self.max_failures:
            self._failures.pop(address, None)
            self._blocked[address] = now + self.block_seconds
            self._trim(now)
            return True
        self._failures[address] = recent
        self._trim(now)
        return False

    def succeeded(self, address: str | None) -> None:
        if address is not None:
            self._failures.pop(address, None)

    def _trim(self, now: float) -> None:
        if len(self._failures) + len(self._blocked) <= MAX_TRACKED:
            return
        for address in [a for a, until in self._blocked.items() if until <= now]:
            del self._blocked[address]
        for address in [a for a, times in self._failures.items() if now - times[-1] >= WINDOW_SECONDS]:
            del self._failures[address]
        while len(self._failures) + len(self._blocked) > MAX_TRACKED and self._failures:
            del self._failures[next(iter(self._failures))]  # the oldest first
