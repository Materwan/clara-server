"""Which models people may use, what each one costs, and which one each person talks to.

A model is named `provider:model` (`local:llama3.2`, `cloud:gpt-oss:120b`, `gemini:gemini-flash-latest`): the
same name can exist at two providers, and every provider that has its key can run at the same time (the server's
own provider and model, `/provider` and `/model`, stay the default).

* **The catalogue.** Everything the providers offer is listed for the administrators, who select the models
  users may choose (`enabled`). Users only ever see those.
* **The weight** is what a token of a model costs, in credits (limits.py). A model of `REFERENCE_B` billion
  parameters weighs 1, one twice as big 2, one of 1 billion 0.125. The size comes from what the provider says
  (Ollama's list) or from the name (`gpt-oss:120b`); a model whose size is unknown (the Gemini, DeepSeek and
  Mistral APIs do not say) weighs 1. An administrator can set any weight by hand.
* **The choice.** Each person picks a model for each surface (web, app, cli, console), from the enabled ones.
  Without a valid choice they get the server's own model. Discord has no choice of its own: an administrator
  sets the model of the whole surface.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .memory import Memory
from .providers import ProviderError, ProviderManager, make_ref, split_ref

log = logging.getLogger(__name__)

REFERENCE_B = 8.0  # billions of parameters of a model that weighs 1
MIN_WEIGHT = 0.01
MAX_WEIGHT = 1_000.0
CACHE_SECONDS = 60.0  # how long what the providers offered is kept
DISCORD = "discord"
DISCORD_OPTION = "models.discord"

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmbt])\b", re.IGNORECASE)
_UNITS = {"k": 1e-6, "m": 1e-3, "b": 1.0, "t": 1e3}
_IN_NAME = re.compile(r"(?<![A-Za-z0-9.])(?:(\d+)x)?(\d+(?:\.\d+)?)b(?![A-Za-z])", re.IGNORECASE)


def parse_size(text: str | None) -> float | None:
    """A size a provider reports ("7.2B", "134.52M") in billions of parameters; None when it says nothing usable."""
    match = _SIZE.match(text or "")
    if match is None:
        return None
    size = float(match.group(1)) * _UNITS[match.group(2).lower()]
    return size if size > 0 else None


def size_from_name(name: str) -> float | None:
    """The size a model's name gives ("gpt-oss:120b" is 120, "mixtral:8x7b" 56), or None."""
    match = _IN_NAME.search(name)
    if match is None:
        return None
    size = float(match.group(2)) * int(match.group(1) or 1)
    return size if size > 0 else None


def auto_weight(size_b: float | None, reference: float = REFERENCE_B) -> float:
    """Credits per token worked out from a size (1 when it is not known)."""
    if size_b is None or reference <= 0:
        return 1.0
    return min(MAX_WEIGHT, max(MIN_WEIGHT, round(size_b / reference, 3)))


def show_weight(weight: float) -> str:
    return f"{weight:.3f}".rstrip("0").rstrip(".") or "0"


@dataclass(frozen=True)
class ModelInfo:
    ref: str
    provider: str
    name: str
    size_b: float | None  # billions of parameters, when known
    enabled: bool  # may users choose it?
    override: float | None  # the weight an administrator set (None: worked out)
    weight: float  # the credits a token costs
    listed: bool = True  # the provider offers it now

    def describe(self, provider_label: str = "") -> dict:
        return {
            "ref": self.ref, "provider": self.provider, "provider_label": provider_label or self.provider,
            "name": self.name, "size_b": self.size_b, "enabled": self.enabled, "weight": self.weight,
            "auto_weight": self.override is None, "listed": self.listed,
        }


@dataclass(frozen=True)
class Chosen:
    """The model a turn runs on: its name, what a token of it costs, and the room it has."""

    ref: str | None  # None: the agent's own backend (no catalogue)
    weight: float = 1.0
    window: int | None = None  # None: the agent's own


class ModelCatalog:
    def __init__(
        self,
        memory: Memory,
        providers: ProviderManager,
        reference: float = REFERENCE_B,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._memory = memory
        self._providers = providers
        self.reference = reference
        self._clock = clock
        self._listed: dict[str, list[str]] = {}  # provider -> the names it offered
        self.problems: dict[str, str] = {}  # provider -> why it could not be listed
        self._fresh_until = 0.0
        self._refresh_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # What is stored
    # ------------------------------------------------------------------
    def _row(self, ref: str):
        with self._memory.lock:
            return self._memory.database.execute("SELECT * FROM models WHERE ref = ?", (ref,)).fetchone()

    def _rows(self) -> dict[str, object]:
        with self._memory.lock:
            return {row["ref"]: row for row in self._memory.database.execute("SELECT * FROM models").fetchall()}

    def _upsert(self, ref: str, **fields) -> None:
        unknown = set(fields) - {"enabled", "weight", "size_b"}
        if unknown:  # the names go into the SQL: only ours
            raise ValueError(f"Unknown model fields: {', '.join(sorted(unknown))}")
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{name} = excluded.{name}" for name in fields)
        with self._memory.lock, self._memory.database as db:
            db.execute(
                f"INSERT INTO models (ref, {columns}) VALUES (?, {marks}) ON CONFLICT (ref) DO UPDATE SET {updates}",
                (ref, *fields.values()),
            )

    def _info(self, ref: str, row, listed: bool = True) -> ModelInfo:
        provider, name = split_ref(ref)
        size = row["size_b"] if row is not None and row["size_b"] else size_from_name(name)
        override = row["weight"] if row is not None else None
        weight = override if override is not None else auto_weight(size, self.reference)
        return ModelInfo(ref, provider, name, size, bool(row["enabled"]) if row is not None else False, override,
                         weight, listed)

    def info(self, ref: str) -> ModelInfo:
        return self._info(ref, self._row(ref))

    def weight(self, ref: str) -> float:
        """Credits a token of this model costs."""
        try:
            return self.info(ref).weight
        except ProviderError:
            return 1.0

    # ------------------------------------------------------------------
    # What the providers offer (administrators)
    # ------------------------------------------------------------------
    async def refresh(self, force: bool = False) -> None:
        """Ask every provider that can be used what it offers (at most once a minute unless `force`); the sizes
        they give are kept, so that the weights are right when a provider is down."""
        async with self._refresh_lock:
            if not force and self._clock() < self._fresh_until:
                return
            usable = [name for name, config in self._providers.configs.items() if config.usable]

            async def ask(provider: str) -> None:
                try:
                    names = await self._providers.list_models_of(provider)
                    try:
                        sizes = await self._providers.sizes_of(provider)
                    except Exception:
                        sizes = {}
                except Exception as error:
                    self.problems[provider] = f"{type(error).__name__}: {str(error)[:200]}"
                    return
                self.problems.pop(provider, None)
                self._listed[provider] = names
                for name in names:
                    size = parse_size(sizes.get(name))
                    if size is not None:
                        self._upsert(make_ref(provider, name), size_b=size)

            await asyncio.gather(*(ask(provider) for provider in usable))
            for provider in list(self._listed):
                if provider not in usable:
                    self._listed.pop(provider)
            self._fresh_until = self._clock() + CACHE_SECONDS

    def provider_label(self, provider: str) -> str:
        config = self._providers.configs.get(provider)
        return config.label if config else provider

    async def listing(self, force: bool = False) -> list[ModelInfo]:
        """Every model of every provider that can be used, with the ones already selected or the server runs
        even when their provider did not answer; by provider, then name."""
        await self.refresh(force)
        rows = self._rows()
        refs: dict[str, bool] = {}
        for provider, names in self._listed.items():
            for name in names:
                refs[make_ref(provider, name)] = True
        for ref, row in rows.items():
            try:
                if ref not in refs and row["enabled"] and self._providers.usable_ref(ref):
                    refs[ref] = False
            except ProviderError:
                continue
        refs.setdefault(self._providers.default_ref, False)
        return sorted(
            (self._info(ref, rows.get(ref), listed) for ref, listed in refs.items() if self._providers.usable_ref(ref)),
            key=lambda info: (info.provider, info.name),
        )

    # ------------------------------------------------------------------
    # What administrators decide
    # ------------------------------------------------------------------
    def _checked(self, ref: str) -> str:
        provider, name = split_ref(ref)
        if provider not in self._providers.configs:
            raise ProviderError(f"Unknown provider {provider!r}. Choose: {', '.join(self._providers.configs)}.")
        return make_ref(provider, name)

    def set_enabled(self, refs: list[str], enabled: bool) -> None:
        for ref in refs:
            self._upsert(self._checked(ref), enabled=int(enabled))

    def set_weight(self, ref: str, weight: float | None) -> None:
        """A weight by hand; None goes back to the one worked out from the size."""
        if weight is not None and not MIN_WEIGHT <= weight <= MAX_WEIGHT:
            raise ProviderError(f"A weight is between {MIN_WEIGHT:g} and {MAX_WEIGHT:g} credits per token.")
        self._upsert(self._checked(ref), weight=weight)

    def discord_model(self) -> str | None:
        """The model of the Discord surface (None: the server's own)."""
        ref = self._memory.option(DISCORD_OPTION)
        return ref if ref and self._providers.usable_ref(ref) else None

    def set_discord_model(self, ref: str | None) -> None:
        if ref is None:
            self._memory.set_option(DISCORD_OPTION, "")
            return
        ref = self._checked(ref)
        if not self._providers.usable_ref(ref):
            config = self._providers.configs[split_ref(ref)[0]]
            raise ProviderError(f"{config.label} needs {config.key_name} in the server environment.")
        self._memory.set_option(DISCORD_OPTION, ref)

    # ------------------------------------------------------------------
    # What users see and choose
    # ------------------------------------------------------------------
    def usable(self) -> list[ModelInfo]:
        """The models a user may choose: those an administrator selected whose provider can be used."""
        rows = self._rows()
        found = []
        for ref, row in rows.items():
            if row["enabled"] and self._providers.usable_ref(ref):
                found.append(self._info(ref, row, listed=True))
        return sorted(found, key=lambda info: (info.provider, info.name))

    def is_usable(self, ref: str) -> bool:
        row = self._row(ref)
        return row is not None and bool(row["enabled"]) and self._providers.usable_ref(ref)

    def choices_of(self, person_id: int) -> dict[str, str]:
        """What a person chose, by surface (a choice that is no longer allowed is not told)."""
        with self._memory.lock:
            rows = self._memory.database.execute(
                "SELECT surface, model FROM model_choices WHERE person_id = ?", (person_id,)
            ).fetchall()
        return {row["surface"]: row["model"] for row in rows if self.is_usable(row["model"])}

    def set_choice(self, person_id: int, surface: str, ref: str | None) -> None:
        """A person's model for a surface; None: the server's own. Only a model an administrator selected."""
        with self._memory.lock, self._memory.database as db:
            if ref is None:
                db.execute("DELETE FROM model_choices WHERE person_id = ? AND surface = ?", (person_id, surface))
                return
            ref = self._checked(ref)
            if not self.is_usable(ref):
                raise ProviderError(f"{ref} is not one of the models you may choose.")
            db.execute(
                "INSERT INTO model_choices (person_id, surface, model) VALUES (?, ?, ?)"
                " ON CONFLICT (person_id, surface) DO UPDATE SET model = excluded.model",
                (person_id, surface, ref),
            )

    def effective_ref(self, surface: str, person_id: int | None) -> str:
        """The model this person is answered by on this surface."""
        if surface == DISCORD:
            return self.discord_model() or self._providers.default_ref
        if person_id is not None:
            chosen = self.choices_of(person_id).get(surface)
            if chosen:
                return chosen
        return self._providers.default_ref

    def choose(self, surface: str, person_id: int | None) -> Chosen:
        ref = self.effective_ref(surface, person_id)
        return Chosen(ref, self.weight(ref), self._providers.window_of(ref))
