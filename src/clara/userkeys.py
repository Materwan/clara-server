"""The API keys people bring: a person saves their own key for a provider the server already knows, and the models of
that provider then run on their key instead of the server's.

* **Where.** A key is kept encrypted (integrations/vault.py, the same secret key as the connected accounts) in
  `user_api_keys`, one per person and provider. The API never gives it back: only its last characters (`hint`).
* **When.** A person who saved a key for a provider is answered with it, on every surface but Discord, by every model
  of that provider, whether it is one an administrator selected (models.py) or not; without a key, the server's key
  and the daily credits apply. Removing the key goes back to that.
* **What it costs.** Nothing in credits (limits.py never sees these answers, and a person out of credits can still use
  their key). Their tokens are logged apart (`usage_log.own_key`, usagelog.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .integrations.vault import Vault, VaultError
from .memory import Memory
from .providers import ProviderManager

MIN_KEY = 8
MAX_KEY = 512
MODELS_CACHE_SECONDS = 60.0
HINT_CHARS = 4


class UserKeyError(ValueError):
    """A key that cannot be saved (unknown provider, empty, refused by the provider)."""


@dataclass(frozen=True)
class SavedKey:
    provider: str
    hint: str
    created_at: str


class UserKeys:
    def __init__(self, memory: Memory, vault: Vault, providers: ProviderManager, clock=time.monotonic):
        self._memory = memory
        self._vault = vault
        self._providers = providers
        self._clock = clock
        self._listed: dict[tuple[int, str], tuple[float, list[str]]] = {}

    # ------------------------------------------------------------------
    # Which providers take a key
    # ------------------------------------------------------------------
    def providers(self) -> list[dict]:
        """The providers a person can bring a key for (those that need one, the server's local host does not)."""
        return [
            {"id": config.id, "label": config.label}
            for config in self._providers.configs.values()
            if config.needs_key
        ]

    def _config(self, provider: str):
        config = self._providers.configs.get(provider)
        if config is None or not config.needs_key:
            raise UserKeyError(f"{provider!r} is not a provider you can bring a key for.")
        return config

    # ------------------------------------------------------------------
    # Saving and forgetting
    # ------------------------------------------------------------------
    def saved(self, person_id: int) -> dict[str, SavedKey]:
        with self._memory.lock:
            rows = self._memory.database.execute(
                "SELECT provider, hint, created_at FROM user_api_keys WHERE person_id = ?", (person_id,)
            ).fetchall()
        return {row["provider"]: SavedKey(row["provider"], row["hint"], row["created_at"]) for row in rows}

    def has(self, person_id: int, provider: str) -> bool:
        with self._memory.lock:
            row = self._memory.database.execute(
                "SELECT 1 FROM user_api_keys WHERE person_id = ? AND provider = ?", (person_id, provider)
            ).fetchone()
        return row is not None

    def key_of(self, person_id: int, provider: str) -> str | None:
        """The person's key for a provider, or None (also when it can no longer be read: the secret key changed)."""
        with self._memory.lock:
            row = self._memory.database.execute(
                "SELECT secret FROM user_api_keys WHERE person_id = ? AND provider = ?", (person_id, provider)
            ).fetchone()
        if row is None:
            return None
        try:
            return self._vault.open(row["secret"]) or None
        except VaultError:
            return None

    async def save(self, person_id: int, provider: str, key: str) -> SavedKey:
        """Check the key with the provider, then keep it (replacing the person's earlier key for it)."""
        config = self._config(provider)
        key = key.strip()
        if not MIN_KEY <= len(key) <= MAX_KEY or any(c.isspace() for c in key):
            raise UserKeyError("That does not look like an API key.")
        try:
            await self._providers.verify_key(provider, key)
        except TimeoutError:
            raise UserKeyError(f"{config.label} did not answer: try again in a moment.") from None
        except PermissionError as error:
            raise UserKeyError(f"{config.label} refused this key: {error}") from None
        except Exception as error:
            raise UserKeyError(f"{config.label} could not check this key: {type(error).__name__}: {str(error)[:200]}") from None
        hint = key[-HINT_CHARS:] if len(key) >= 3 * HINT_CHARS else ""
        created = datetime.now(UTC).isoformat(timespec="seconds")
        with self._memory.lock, self._memory.database as db:
            db.execute(
                "INSERT INTO user_api_keys (person_id, provider, secret, hint, created_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (person_id, provider) DO UPDATE SET secret = excluded.secret, hint = excluded.hint,"
                " created_at = excluded.created_at",
                (person_id, provider, self._vault.seal(key), hint, created),
            )
        self._listed.pop((person_id, provider), None)
        return SavedKey(provider, hint, created)

    def remove(self, person_id: int, provider: str) -> bool:
        with self._memory.lock, self._memory.database as db:
            removed = db.execute(
                "DELETE FROM user_api_keys WHERE person_id = ? AND provider = ?", (person_id, provider)
            ).rowcount
        self._listed.pop((person_id, provider), None)
        return bool(removed)

    # ------------------------------------------------------------------
    # What a key can run
    # ------------------------------------------------------------------
    async def models(self, person_id: int, provider: str) -> list[str]:
        """Every model the provider offers to the person's key (kept a minute)."""
        slot = (person_id, provider)
        cached = self._listed.get(slot)
        if cached is not None and self._clock() < cached[0]:
            return cached[1]
        key = self.key_of(person_id, provider)
        if key is None:
            return []
        try:
            names = sorted(await self._providers.list_models_with_key(provider, key))
        except Exception:  # the provider is down or refuses the key: no model is offered, nothing breaks
            return []
        self._listed[slot] = (self._clock() + MODELS_CACHE_SECONDS, names)
        return names
