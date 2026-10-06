"""Asking the model again when its service is overloaded or unreachable (Gemini often is).

Only a failure before the model has said anything is retried: once a piece of the answer has been passed on,
asking again would repeat it. The wait doubles each time, with a little jitter so that several turns do not
come back together, and gives way to the `Retry-After` the service sent.
"""

from __future__ import annotations

import random

import httpx

RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
MAX_WAIT = 60.0  # seconds: a `Retry-After` longer than this is not waited for


def retryable(error: BaseException) -> bool:
    """Is this failure the kind that passes: the service is busy or could not be reached?"""
    status = getattr(error, "status", None) or getattr(error, "status_code", None)  # LlmError, ollama.ResponseError
    if isinstance(status, int):
        return status in RETRY_STATUSES
    if isinstance(error, httpx.TimeoutException):
        return isinstance(error, httpx.ConnectTimeout)  # a model that went silent is not asked again
    return isinstance(error, (httpx.TransportError, ConnectionError))


def delay_before(error: BaseException, attempt: int, base: float) -> float | None:
    """Seconds to wait before attempt number `attempt + 1` (`attempt` starts at 0), or None to give up
    because the service wants us to wait longer than we would."""
    asked = getattr(error, "retry_after", None)
    if isinstance(asked, (int, float)) and asked > 0:
        return float(asked) if asked <= MAX_WAIT else None
    wait = min(MAX_WAIT, base * 2**attempt)
    return wait * random.uniform(0.75, 1.25)
