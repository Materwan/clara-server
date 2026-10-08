"""The API keys people bring: stored encrypted, checked with the provider, and answers on them cost no credits."""

import pytest
from conftest import FakeBackend, say
from fastapi.testclient import TestClient

from clara.agent import Agent, ChatRequest
from clara.integrations.vault import Vault
from clara.limits import UsageLimits
from clara.models import ModelCatalog
from clara.prompt import SystemPrompt
from clara.providers import ProviderManager
from clara.server import create_app
from clara.tools import default_toolbox
from clara.usagelog import UsageLog
from clara.userkeys import UserKeyError, UserKeys

PASSWORD = "correct horse battery"
GOOD = "AIza-this-is-a-good-key-1234"
OTHER = "AIza-another-good-key-5678"


class KeyFarm:
    """A fake backend per (provider, model, key): remembers which key each one was made with."""

    def __init__(self):
        self.backends: dict[tuple[str, str, str | None], FakeBackend] = {}
        self.refused = {"refused-key-000000"}

    def __call__(self, config, model):
        slot = (config.id, model, config.api_key)
        if slot not in self.backends:
            backend = FakeBackend(*[say("ok") for _ in range(40)], model=model)
            refused = self.refused

            async def verify(key=config.api_key):
                if key in refused:
                    raise PermissionError("HTTP 401")

            backend.verify = verify
            self.backends[slot] = backend
        return self.backends[slot]

    def used_with(self, key: str) -> list[FakeBackend]:
        return [b for (_, _, k), b in self.backends.items() if k == key and b.calls]


@pytest.fixture
def farm():
    return KeyFarm()


@pytest.fixture
def providers(settings, farm):
    return ProviderManager.from_settings(settings, factory=farm)


@pytest.fixture
def keys(memory, providers, tmp_path):
    return UserKeys(memory, Vault("test-secret"), providers)


@pytest.fixture
def catalog(memory, providers, keys):
    catalog = ModelCatalog(memory, providers)
    catalog.user_keys = keys
    return catalog


def agent_for(memory, tmp_path, providers, catalog, limits=None, usage_log=None) -> Agent:
    return Agent(
        memory, providers, default_toolbox(), SystemPrompt(tmp_path / "none.md"),
        context_window=lambda: providers.context_window, limits=limits, models=catalog, usage_log=usage_log,
    )


async def run(agent: Agent, **fields) -> list[dict]:
    request = ChatRequest(**{"surface": "web", "user_id": "erwan", "user_name": "Erwan", "message": "hi", **fields})
    return [event async for event in agent.turn(request)]


# --- storing -----------------------------------------------------------------------------------------------


async def test_a_key_is_checked_with_the_provider_then_kept_encrypted(memory, keys):
    person = memory.resolve("web", "erwan", "Erwan")
    with pytest.raises(UserKeyError, match="refused"):
        await keys.save(person.id, "gemini", "refused-key-000000")
    assert keys.saved(person.id) == {}

    saved = await keys.save(person.id, "gemini", GOOD)
    assert saved.hint == GOOD[-4:]
    assert keys.key_of(person.id, "gemini") == GOOD
    with memory.lock:
        stored = memory.database.execute("SELECT secret FROM user_api_keys").fetchone()["secret"]
    assert GOOD not in stored and stored  # the database holds it encrypted

    await keys.save(person.id, "gemini", OTHER)  # replaces
    assert keys.key_of(person.id, "gemini") == OTHER
    assert keys.remove(person.id, "gemini") and not keys.remove(person.id, "gemini")
    assert keys.key_of(person.id, "gemini") is None


async def test_only_providers_that_need_a_key_take_one(memory, keys):
    person = memory.resolve("web", "erwan", "Erwan")
    with pytest.raises(UserKeyError, match="not a provider"):
        await keys.save(person.id, "local", GOOD)
    with pytest.raises(UserKeyError, match="not a provider"):
        await keys.save(person.id, "nowhere", GOOD)
    with pytest.raises(UserKeyError, match="look like"):
        await keys.save(person.id, "gemini", "short")
    assert {p["id"] for p in keys.providers()} == {"cloud", "gemini", "deepseek", "mistral"}


async def test_a_key_follows_the_person_when_two_people_are_merged_and_goes_when_they_are_erased(memory, keys):
    first, second = memory.resolve("web", "erwan", "Erwan"), memory.resolve("discord", "42", "Erwan")
    await keys.save(second.id, "mistral", GOOD)
    await keys.save(first.id, "gemini", OTHER)
    memory.link_account("discord", "42", first, force=True)
    assert keys.key_of(first.id, "mistral") == GOOD and keys.key_of(first.id, "gemini") == OTHER
    memory.delete_person(first.id)
    assert memory.database.execute("SELECT COUNT(*) FROM user_api_keys").fetchone()[0] == 0


# --- answering ---------------------------------------------------------------------------------------------


async def test_a_person_with_a_key_is_answered_on_it_for_free_whatever_the_model_and_the_limit(
    memory, tmp_path, providers, catalog, keys, farm
):
    limits = UsageLimits(memory, default=5)  # 5 credits a day: one server answer (13 tokens) passes it
    usage_log = UsageLog(memory)
    agent = agent_for(memory, tmp_path, providers, catalog, limits, usage_log)
    person = memory.resolve("web", "erwan", "Erwan")
    assert (await run(agent))[-1]["own_key"] is False  # the server's key: credits
    assert limits.used(person.id) > 0
    with pytest.raises(Exception, match="credits for today"):
        await run(agent)

    await keys.save(person.id, "gemini", GOOD)
    catalog.set_choice(person.id, "web", "gemini:gemini-anything")  # not a model an administrator selected
    before = limits.used(person.id)
    done = (await run(agent))[-1]
    assert (done["model_ref"], done["own_key"], done["credits"], done["weight"]) == ("gemini:gemini-anything", True, 0, 0.0)
    assert limits.used(person.id) == before  # no credits counted, and the limit did not stop it
    [backend] = farm.used_with(GOOD)
    assert backend.model == "gemini-anything"

    mine = usage_log.per_user(person_id=person.id, own_key=True)
    assert [(e["prompt_tokens"], e["completion_tokens"], e["credits"]) for e in mine] == [(10, 3, 0)]
    assert usage_log.per_user(person_id=person.id)[0]["credits"] > 0  # the server's figures leave it out
    assert sum(e["prompt_tokens"] for e in usage_log.per_user(person_id=person.id)) == 10  # only the first answer
    assert usage_log.history(person.id, own_key=True)["calls"][0]["own_key"] is True


async def test_a_key_is_only_used_for_its_own_provider_and_never_on_discord(
    memory, tmp_path, providers, catalog, keys, farm
):
    agent = agent_for(memory, tmp_path, providers, catalog, UsageLimits(memory))
    person = memory.resolve("discord", "42", "Erwan")
    memory.link_account("web", "erwan", person)
    await keys.save(person.id, "gemini", GOOD)
    await keys.save(person.id, "cloud", OTHER)

    assert (await run(agent))[-1]["own_key"] is False  # the server's own model is local: no key applies
    catalog.set_discord_model("cloud:fake-big")
    discord = (await run(agent, surface="discord", user_id="42"))[-1]
    assert discord["own_key"] is False and discord["credits"] > 0  # shared surface: the server's key, in credits
    assert not farm.used_with(OTHER) and not farm.used_with(GOOD)

    catalog.set_choice(person.id, "web", "cloud:fake-big")
    web = (await run(agent))[-1]  # the same model on the web site runs on the person's key for that provider
    assert web["own_key"] is True and web["credits"] == 0
    assert farm.used_with(OTHER) and not farm.used_with(GOOD)  # not the key of another provider


async def test_without_a_key_a_model_nobody_selected_is_refused_and_removing_the_key_goes_back(
    memory, providers, catalog, keys
):
    person = memory.resolve("web", "erwan", "Erwan")
    from clara.providers import ProviderError

    with pytest.raises(ProviderError, match="not one of the models"):
        catalog.set_choice(person.id, "web", "gemini:gemini-anything")
    await keys.save(person.id, "gemini", GOOD)
    catalog.set_choice(person.id, "web", "gemini:gemini-anything")
    assert catalog.choices_of(person.id) == {"web": "gemini:gemini-anything"}
    keys.remove(person.id, "gemini")
    assert catalog.choices_of(person.id) == {}  # the choice is not told any more
    assert catalog.choose("web", person.id).ref == "local:fake"


# --- the API -----------------------------------------------------------------------------------------------


@pytest.fixture
def http(settings, farm):
    with TestClient(create_app(settings, ProviderManager.from_settings(settings, factory=farm))) as client:
        client.app.state.users.create("root", PASSWORD, admin=True)
        client.app.state.users.create("erwan", PASSWORD)
        yield client


def bearer(http, name="erwan", surface="web") -> dict:
    answer = http.post("/v1/auth/login", json={"username": name, "password": PASSWORD, "surface": surface})
    return {"Authorization": f"Bearer {answer.json()['token']}"}


def test_a_user_saves_lists_and_removes_a_key_and_the_key_is_never_given_back(http):
    erwan = bearer(http)
    state = http.get("/v1/me/api-keys", headers=erwan).json()
    assert {p["id"] for p in state["providers"]} == {"cloud", "gemini", "deepseek", "mistral"}
    assert not any(p["saved"] for p in state["providers"])

    refused = http.put("/v1/me/api-keys/gemini", json={"api_key": "refused-key-000000"}, headers=erwan)
    assert refused.status_code == 422 and "refused" in refused.json()["detail"]
    assert http.put("/v1/me/api-keys/local", json={"api_key": GOOD}, headers=erwan).status_code == 422

    saved = http.put("/v1/me/api-keys/gemini", json={"api_key": GOOD}, headers=erwan)
    assert saved.status_code == 200 and GOOD not in saved.text
    [gemini] = [p for p in saved.json()["providers"] if p["id"] == "gemini"]
    assert gemini["saved"] and gemini["hint"] == GOOD[-4:]
    assert GOOD not in http.get("/v1/me/api-keys", headers=erwan).text

    assert http.delete("/v1/me/api-keys/gemini", headers=erwan).status_code == 200
    assert http.delete("/v1/me/api-keys/gemini", headers=erwan).status_code == 404


def test_a_key_belongs_to_one_person_and_the_models_list_shows_what_it_opens(http, farm):
    erwan, root = bearer(http), bearer(http, "root")
    http.put("/v1/me/api-keys/gemini", json={"api_key": GOOD}, headers=erwan)
    seen = http.get("/v1/models", params={"surface": "web", "user_id": "erwan"}, headers=erwan).json()
    assert {m["ref"] for m in seen["personal"]} == {"gemini:fake", "gemini:fake-big", "gemini:other-model"}
    assert all(m["weight"] == 0 and m["own_key"] for m in seen["personal"])
    discord = http.get("/v1/models", params={"surface": "discord", "user_id": "erwan"}, headers=erwan)
    assert discord.status_code in (200, 403)
    if discord.status_code == 200:
        assert discord.json()["personal"] == []

    chosen = http.put(
        "/v1/models/choice", json={"surface": "web", "user_id": "erwan", "model": "gemini:fake-big"}, headers=erwan
    ).json()
    assert chosen["current"]["ref"] == "gemini:fake-big" and chosen["current"]["own_key"] is True

    others = http.get("/v1/models", params={"surface": "web", "user_id": "root"}, headers=root).json()
    assert others["personal"] == []
    assert http.get("/v1/me/api-keys", headers=root).json()["providers"][1]["saved"] is False
