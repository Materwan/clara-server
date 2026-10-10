"""Models: what a token costs (the weight), which ones users may choose, and which one each person talks to."""

import time

import pytest
from conftest import FakeBackend, say
from fastapi.testclient import TestClient

from clara.agent import Agent, ChatRequest
from clara.commands import CommandContext, registry
from clara.limits import UsageLimits, credits_for
from clara.models import Capabilities, ModelCatalog, auto_weight, parse_size, size_from_name
from clara.prompt import SystemPrompt
from clara.providers import ProviderError, ProviderManager, make_ref, split_ref
from clara.server import create_app
from clara.tools import default_toolbox
from clara.users import Users

PASSWORD = "correct horse battery"
TURN = 13  # what say() reports: 10 prompt tokens and 3 completion tokens


class Farm:
    """One fake backend per `provider:model`, made when first asked for, with the sizes a provider would report and
    the capabilities it would say (`capabilities`: provider -> model -> what it says)."""

    def __init__(
        self, sizes: dict[str, dict[str, str]] | None = None, capabilities: dict[str, dict[str, dict]] | None = None
    ):
        self.backends: dict[str, FakeBackend] = {}
        self.sizes = sizes or {}
        self.capabilities = capabilities or {}

    def __call__(self, config, model):
        ref = make_ref(config.id, model)
        if ref not in self.backends:
            backend = FakeBackend(*[say("ok") for _ in range(40)], model=model)
            sizes = self.sizes.get(config.id)
            if sizes is not None:
                async def model_sizes(sizes=sizes):
                    return sizes

                backend.model_sizes = model_sizes
            said = self.capabilities.get(config.id)
            if said is not None:
                async def model_capabilities(names, said=said):
                    return {name: said[name] for name in names if name in said}

                backend.model_capabilities = model_capabilities
            self.backends[ref] = backend
        return self.backends[ref]


def make_providers(settings, farm: Farm) -> ProviderManager:
    return ProviderManager.from_settings(settings, factory=farm)


# --- sizes and weights -------------------------------------------------------------------------------------


def test_a_size_is_read_from_what_a_provider_says_and_from_a_name():
    assert [parse_size(text) for text in ("7.2B", "134.52M", "1T", " 8b", "", None, "huge")] == [
        7.2, pytest.approx(0.13452), 1000.0, 8.0, None, None, None,
    ]
    assert [size_from_name(name) for name in ("gpt-oss:120b", "llama3.2:3b", "qwen3:0.6b", "mixtral:8x7b",
                                              "deepseek-r1:671b-cloud", "gemma3:27b-it-qat", "llama3.2",
                                              "gemini-flash-latest", "mistral-7b-instruct-v0.3")] == [
        120.0, 3.0, 0.6, 56.0, 671.0, 27.0, None, None, 7.0,
    ]


def test_a_weight_follows_the_size_and_is_one_when_the_size_is_not_known():
    assert auto_weight(8) == 1.0
    assert auto_weight(120) == 15.0
    assert auto_weight(1) == 0.125
    assert auto_weight(None) == 1.0
    assert auto_weight(0.0001) == 0.01  # never free
    assert auto_weight(16, reference=4) == 4.0


def test_credits_are_tokens_times_the_weight_and_any_use_costs_at_least_one():
    assert credits_for(100) == 100
    assert credits_for(100, 15) == 1500
    assert credits_for(100, 0.125) == 13  # rounded up
    assert credits_for(1, 0.01) == 1
    assert credits_for(0, 5) == 0


def test_a_model_is_named_provider_colon_model():
    assert make_ref("cloud", "gpt-oss:120b") == "cloud:gpt-oss:120b"
    assert split_ref("cloud:gpt-oss:120b") == ("cloud", "gpt-oss:120b")
    for bad in ("", "cloud", "cloud:", ":x"):
        with pytest.raises(ProviderError):
            split_ref(bad)


# --- the catalogue -----------------------------------------------------------------------------------------


@pytest.fixture
def farm():
    return Farm({"local": {"fake": "3.2B", "other-model": "70B"}})


@pytest.fixture
def providers(settings, farm):
    return make_providers(settings, farm)


@pytest.fixture
def catalog(memory, providers):
    return ModelCatalog(memory, providers)


async def test_the_catalogue_lists_every_provider_that_can_be_used_with_the_sizes_they_say(catalog):
    found = {info.ref: info for info in await catalog.listing()}
    assert {"local:fake", "local:fake-big", "local:other-model", "cloud:fake", "cloud:other-model"} <= set(found)
    assert not any(ref.startswith("gemini:") for ref in found)  # no key: not listed
    assert found["local:other-model"].size_b == 70 and found["local:other-model"].weight == 8.75
    assert found["local:fake"].weight == 0.4
    assert found["cloud:fake"].weight == 1.0  # this provider says no size and the name has none
    assert not any(info.enabled for info in found.values())  # nothing is selectable until an administrator says so


async def test_a_provider_that_does_not_answer_is_reported_and_the_others_are_still_listed(settings, memory):
    class Down(FakeBackend):
        async def list_models(self):
            raise ConnectionError("no route")

    def factory(config, model):
        return Down(model=model) if config.id == "cloud" else FakeBackend(model=model)

    catalog = ModelCatalog(memory, ProviderManager.from_settings(settings, factory=factory))
    found = await catalog.listing()
    assert any(info.ref == "local:fake" for info in found)
    assert "ConnectionError" in catalog.problems["cloud"]


async def test_administrators_select_models_and_set_weights(catalog, memory):
    await catalog.listing()
    catalog.set_enabled(["local:other-model", "cloud:fake"], True)
    assert [info.ref for info in catalog.usable()] == ["cloud:fake", "local:other-model"]
    catalog.set_enabled(["cloud:fake"], False)
    assert [info.ref for info in catalog.usable()] == ["local:other-model"]
    catalog.set_weight("local:other-model", 2.5)
    assert catalog.weight("local:other-model") == 2.5
    assert catalog.info("local:other-model").override == 2.5
    catalog.set_weight("local:other-model", None)  # back to the size
    assert catalog.weight("local:other-model") == 8.75
    for bad in (0, -1, 5000):
        with pytest.raises(ProviderError):
            catalog.set_weight("local:fake", bad)
    with pytest.raises(ProviderError):
        catalog.set_enabled(["nowhere:model"], True)


async def test_the_sizes_a_provider_gave_are_kept_when_it_is_down_later(settings, memory, farm):
    first = ModelCatalog(memory, make_providers(settings, farm))
    await first.listing()
    again = ModelCatalog(memory, make_providers(settings, Farm()))  # a restart; this time nobody says a size
    assert again.weight("local:other-model") == 8.75


def test_a_choice_must_be_a_model_users_may_choose_and_belongs_to_a_person_and_a_surface(memory, catalog):
    person = memory.resolve("web", "erwan", "Erwan")
    with pytest.raises(ProviderError):
        catalog.set_choice(person.id, "web", "local:other-model")  # not selected
    catalog.set_enabled(["local:other-model", "cloud:fake"], True)
    catalog.set_choice(person.id, "web", "local:other-model")
    catalog.set_choice(person.id, "cli", "cloud:fake")
    assert catalog.choices_of(person.id) == {"web": "local:other-model", "cli": "cloud:fake"}
    assert catalog.effective_ref("web", person.id) == "local:other-model"
    assert catalog.effective_ref("app", person.id) == "local:fake"  # nothing chosen there: the server's own
    catalog.set_enabled(["local:other-model"], False)  # an administrator takes it away
    assert catalog.effective_ref("web", person.id) == "local:fake"
    assert catalog.choices_of(person.id) == {"cli": "cloud:fake"}
    catalog.set_choice(person.id, "cli", None)
    assert catalog.choices_of(person.id) == {}


def test_discord_has_one_model_set_by_an_administrator_and_ignores_what_people_chose(memory, catalog):
    person = memory.resolve("discord", "42", "Erwan")
    catalog.set_enabled(["cloud:fake"], True)
    catalog.set_choice(person.id, "discord", "cloud:fake")
    assert catalog.effective_ref("discord", person.id) == "local:fake"
    catalog.set_discord_model("cloud:fake-big")  # it need not be one users may choose
    assert catalog.effective_ref("discord", person.id) == "cloud:fake-big"
    assert catalog.effective_ref("web", person.id) == "local:fake"
    catalog.set_discord_model(None)
    assert catalog.discord_model() is None
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        catalog.set_discord_model("gemini:gemini-flash-latest")


def test_a_choice_goes_to_the_person_who_remains_when_two_people_are_merged(memory, catalog):
    first, second = memory.resolve("web", "erwan", "Erwan"), memory.resolve("discord", "42", "Erwan")
    catalog.set_enabled(["local:other-model", "cloud:fake"], True)
    catalog.set_choice(first.id, "web", "local:other-model")
    catalog.set_choice(second.id, "web", "cloud:fake")
    catalog.set_choice(second.id, "cli", "cloud:fake")
    memory.link_account("discord", "42", first, force=True)
    assert catalog.choices_of(first.id) == {"web": "local:other-model", "cli": "cloud:fake"}
    memory.delete_person(first.id)
    assert memory.database.execute("SELECT COUNT(*) FROM model_choices").fetchone()[0] == 0


# --- the agent ---------------------------------------------------------------------------------------------


def make_agent(memory, tmp_path, providers, catalog, limits=None) -> Agent:
    return Agent(
        memory, providers, default_toolbox(), SystemPrompt(tmp_path / "none.md"),
        context_window=lambda: providers.context_window, limits=limits, models=catalog,
    )


async def run(agent: Agent, **fields) -> list[dict]:
    request = ChatRequest(**{"surface": "web", "user_id": "erwan", "user_name": "Erwan", "message": "hi", **fields})
    return [event async for event in agent.turn(request)]


async def test_a_person_is_answered_by_the_model_they_chose_and_pays_its_weight(memory, tmp_path, providers, catalog, farm):
    limits = UsageLimits(memory)
    agent = make_agent(memory, tmp_path, providers, catalog, limits)
    person = memory.resolve("web", "erwan", "Erwan")
    await catalog.listing()  # the sizes the provider says are learned here
    default = (await run(agent))[-1]
    assert (default["model_ref"], default["weight"], default["credits"]) == ("local:fake", 0.4, 6)  # 13 x 0.4, up
    assert limits.used(person.id) == 6

    catalog.set_enabled(["local:other-model"], True)
    catalog.set_choice(person.id, "web", "local:other-model")
    chosen = (await run(agent))[-1]
    assert (chosen["model"], chosen["provider"], chosen["model_ref"]) == ("other-model", "local", "local:other-model")
    assert chosen["weight"] == 8.75 and chosen["credits"] == 114  # 13 x 8.75 = 113.75
    assert limits.used(person.id) == 6 + 114
    assert len(farm.backends["local:other-model"].calls) == 1  # that backend answered, not the server's own
    assert len(farm.backends["local:fake"].calls) == 1


async def test_the_choice_of_one_surface_does_not_change_another(memory, tmp_path, providers, catalog, farm):
    agent = make_agent(memory, tmp_path, providers, catalog)
    person = memory.resolve("web", "erwan", "Erwan")
    memory.link_account("app", "erwan", person)
    catalog.set_enabled(["cloud:other-model"], True)
    catalog.set_choice(person.id, "web", "cloud:other-model")
    assert (await run(agent))[-1]["model_ref"] == "cloud:other-model"
    assert (await run(agent, surface="app"))[-1]["model_ref"] == "local:fake"


async def test_discord_answers_with_the_model_of_the_surface_whoever_asks(memory, tmp_path, providers, catalog):
    agent = make_agent(memory, tmp_path, providers, catalog)
    catalog.set_discord_model("cloud:fake-big")
    done = (await run(agent, surface="discord", user_id="42", user_name="Erwan"))[-1]
    assert done["model_ref"] == "cloud:fake-big"
    observed = (await run(agent, surface="discord", user_id="42", mode="observe"))[-1]
    assert observed["model_ref"] == "cloud:fake-big"


async def test_the_server_default_still_decides_for_those_who_chose_nothing(memory, tmp_path, providers, catalog):
    agent = make_agent(memory, tmp_path, providers, catalog)
    providers.switch("cloud")
    assert (await run(agent))[-1]["model_ref"] == "cloud:fake-big"
    providers.set_model("other-model")
    assert (await run(agent))[-1]["model_ref"] == "cloud:other-model"


async def test_each_model_has_the_window_of_its_provider(memory, tmp_path, settings, farm):
    from dataclasses import replace

    providers = make_providers(replace(settings, local_context_window=4_000, cloud_context_window=9_000), farm)
    catalog = ModelCatalog(memory, providers)
    agent = make_agent(memory, tmp_path, providers, catalog)
    person = memory.resolve("web", "erwan", "Erwan")
    catalog.set_enabled(["cloud:other-model"], True)
    assert (await run(agent))[-1]["context"]["window"] == 4_000
    catalog.set_choice(person.id, "web", "cloud:other-model")
    assert (await run(agent))[-1]["context"]["window"] == 9_000


# --- the API -----------------------------------------------------------------------------------------------


@pytest.fixture
def http(settings, farm):
    with TestClient(create_app(settings, make_providers(settings, farm))) as client:
        users = client.app.state.users
        users.create("root", PASSWORD, admin=True)
        users.create("erwan", PASSWORD)
        yield client


def bearer(http, name="erwan", surface="web") -> dict:
    answer = http.post("/v1/auth/login", json={"username": name, "password": PASSWORD, "surface": surface})
    return {"Authorization": f"Bearer {answer.json()['token']}"}


def listed(http, headers, surface="web", user="erwan") -> dict:
    return http.get("/v1/models", params={"surface": surface, "user_id": user}, headers=headers).json()


def test_users_only_see_the_models_an_administrator_selected(http):
    admin, erwan = bearer(http, "root"), bearer(http)
    assert listed(http, erwan)["models"] == []
    everything = http.get("/v1/admin/catalog", headers=admin).json()
    assert any(m["ref"] == "local:other-model" and not m["enabled"] for m in everything["models"])
    assert {p["id"] for p in everything["providers"]} >= {"local", "cloud"}

    done = http.patch("/v1/admin/catalog", json={"refs": ["local:other-model", "cloud:fake"], "enabled": True}, headers=admin)
    assert done.status_code == 200
    seen = listed(http, erwan)
    assert [m["ref"] for m in seen["models"]] == ["cloud:fake", "local:other-model"]
    assert seen["models"][1]["weight"] == 8.75 and seen["models"][1]["provider_label"] == "Local host"
    assert seen["default"]["ref"] == "local:fake" and seen["current"]["ref"] == "local:fake"

    http.patch("/v1/admin/catalog", json={"refs": ["cloud:fake"], "enabled": False}, headers=admin)
    assert [m["ref"] for m in listed(http, erwan)["models"]] == ["local:other-model"]


def test_a_user_chooses_per_surface_among_the_selected_models_and_only_those(http):
    admin, erwan = bearer(http, "root"), bearer(http)
    http.patch("/v1/admin/catalog", json={"refs": ["local:other-model"], "enabled": True}, headers=admin)
    choice = {"surface": "web", "user_id": "erwan"}
    refused = http.put("/v1/models/choice", json={**choice, "model": "local:fake-big"}, headers=erwan)
    assert refused.status_code == 422 and "not one of the models" in refused.json()["detail"]

    done = http.put("/v1/models/choice", json={**choice, "model": "local:other-model"}, headers=erwan).json()
    assert done["choices"] == {"web": "local:other-model"} and done["current"]["ref"] == "local:other-model"
    other = http.put("/v1/models/choice", json={**choice, "model": "local:other-model", "for_surface": "cli"}, headers=erwan)
    assert other.json()["choices"] == {"web": "local:other-model", "cli": "local:other-model"}
    assert listed(http, erwan)["choices"] == {"web": "local:other-model", "cli": "local:other-model"}
    assert listed(http, bearer(http, "erwan", "app"), surface="app")["current"]["ref"] == "local:fake"

    cleared = http.put("/v1/models/choice", json={**choice, "model": None}, headers=erwan).json()
    assert cleared["choices"] == {"cli": "local:other-model"}


def test_a_user_cannot_choose_for_somebody_else_or_for_discord(http):
    erwan = bearer(http)
    http.patch("/v1/admin/catalog", json={"refs": ["local:other-model"], "enabled": True}, headers=bearer(http, "root"))
    model = "local:other-model"
    assert http.put("/v1/models/choice", json={"surface": "web", "user_id": "root", "model": model}, headers=erwan).status_code == 403
    assert http.put("/v1/models/choice", json={"surface": "cli", "user_id": "erwan", "model": model}, headers=erwan).status_code == 403
    discord = http.put("/v1/models/choice", json={"surface": "web", "user_id": "erwan", "model": model, "for_surface": "discord"}, headers=erwan)
    assert discord.status_code == 403 and "administrator" in discord.json()["detail"]


def test_only_administrators_reach_the_catalogue(http):
    erwan = bearer(http)
    assert http.get("/v1/admin/catalog", headers=erwan).status_code == 403
    assert http.patch("/v1/admin/catalog", json={"refs": ["local:fake"], "enabled": True}, headers=erwan).status_code == 403
    assert http.put("/v1/admin/catalog/discord", json={"model": None}, headers=erwan).status_code == 403


def test_a_weight_is_set_for_one_model_and_can_go_back_to_the_size(http):
    admin = bearer(http, "root")
    patch = lambda **body: http.patch("/v1/admin/catalog", json=body, headers=admin)  # noqa: E731
    found = lambda: {m["ref"]: m for m in http.get("/v1/admin/catalog", headers=admin).json()["models"]}  # noqa: E731
    assert patch(refs=["local:other-model"], weight=3).status_code == 200
    assert found()["local:other-model"]["weight"] == 3 and found()["local:other-model"]["auto_weight"] is False
    assert patch(refs=["local:other-model"], auto_weight=True).status_code == 200
    assert found()["local:other-model"]["weight"] == 8.75 and found()["local:other-model"]["auto_weight"] is True
    assert patch(refs=["local:fake", "local:other-model"], weight=2).status_code == 422
    assert patch(refs=["local:fake"], weight=2, auto_weight=True).status_code == 422
    assert patch(refs=["local:fake"], weight=0).status_code == 422
    assert patch(refs=["nowhere:x"], enabled=True).status_code == 422


def test_an_administrator_sets_the_model_of_discord(http):
    admin = bearer(http, "root")
    done = http.put("/v1/admin/catalog/discord", json={"model": "cloud:fake-big"}, headers=admin)
    assert done.json() == {"discord": "cloud:fake-big", "default": "local:fake"}
    assert http.get("/v1/admin/catalog", headers=admin).json()["discord"] == "cloud:fake-big"
    assert http.put("/v1/admin/catalog/discord", json={"model": "gemini:x"}, headers=admin).status_code == 422
    assert http.put("/v1/admin/catalog/discord", json={"model": None}, headers=admin).json()["discord"] is None


def test_the_chat_answer_says_which_model_answered_and_what_it_cost(http):
    admin, erwan = bearer(http, "root"), bearer(http)
    http.patch("/v1/admin/catalog", json={"refs": ["local:other-model"], "enabled": True}, headers=admin)
    http.put("/v1/models/choice", json={"surface": "web", "user_id": "erwan", "model": "local:other-model"}, headers=erwan)
    answer = http.post("/v1/chat", json={"surface": "web", "user_id": "erwan", "message": "hi"}, headers=erwan).json()
    assert (answer["model_ref"], answer["credits"], answer["quota"]["used"]) == ("local:other-model", 114, 114)


# --- the console -------------------------------------------------------------------------------------------


async def test_the_console_command_selects_models_and_sets_weights_and_discord(settings, memory, tmp_path, providers, catalog):
    agent = make_agent(memory, tmp_path, providers, catalog)
    ctx = CommandContext(settings, memory, agent, providers, time.monotonic(), "127.0.0.1:8765", models=catalog,
                         users=Users(memory))

    async def command(line: str) -> str:
        return (await registry.execute(line, ctx)).output

    shown = await command("/models")
    assert "local:other-model" in shown and "70B" in shown and "8.75" in shown and "Server default: local:fake" in shown
    assert "2 model(s) now selectable" not in shown
    assert "1 model(s) now selectable" in await command("/models enable other-model")  # a bare name: the active provider
    assert [info.ref for info in catalog.usable()] == ["local:other-model"]
    assert "now selectable" in await command("/models enable cloud")  # a whole provider
    assert all(info.provider != "gemini" for info in catalog.usable()) and len(catalog.usable()) > 1
    assert "no longer selectable" in await command("/models disable all")
    assert catalog.usable() == []
    assert "now costs 2.5 credit(s)" in await command("/models weight local:other-model 2.5")
    assert "from its size" in await command("/models weight local:other-model auto")
    assert (await command("/models weight local:other-model lots")).startswith("!")
    assert "Discord answers with cloud:fake-big" in await command("/models discord cloud:fake-big")
    assert catalog.discord_model() == "cloud:fake-big"
    assert "server default" in await command("/models discord default")
    assert (await command("/models enable")).startswith("! Usage")


# --- the terminal ------------------------------------------------------------------------------------------


def offered(*names):
    return {
        "default": {"ref": "local:fake", "name": "fake", "provider_label": "Local host", "weight": 0.4},
        "current": {"ref": "local:fake", "name": "fake", "provider_label": "Local host", "weight": 0.4},
        "models": [{"ref": f"cloud:{n}", "name": n, "provider_label": "Ollama API key", "weight": 8.75} for n in names],
    }


def test_the_terminal_picks_a_model_by_number_name_or_reference():
    from clara.client import pick_model

    info = offered("big", "small")
    assert pick_model(info, "1") == "cloud:big"
    assert pick_model(info, "SMALL") == "cloud:small"
    assert pick_model(info, "cloud:big") == "cloud:big"
    assert pick_model(info, "default") is None and pick_model(info, "0") is None
    for bad in ("9", "nothing", "cloud"):
        with pytest.raises(ValueError, match="not one of the models offered"):
            pick_model(info, bad)


def test_the_terminal_lists_the_models_with_their_cost():
    from clara.client import describe_models

    shown = describe_models(offered("big"))
    assert "0.4 credits per token" in shown and "1. big (Ollama API key), 8.75 credits per token" in shown
    assert "An administrator has not offered other models" in describe_models(offered())


def test_the_terminal_command_chooses_the_model_of_the_cli_surface(http, capsys):
    from clara.client import ClaraApi, command

    admin = bearer(http, "root")
    http.patch("/v1/admin/catalog", json={"refs": ["local:other-model"], "enabled": True}, headers=admin)
    api = ClaraApi("http://testserver", "unused", "erwan", None, "cli:erwan")
    api.http = http
    http.headers.update(bearer(http, "erwan", "cli"))
    assert command(api, "/model 1") is True
    assert "* 1. other-model" in capsys.readouterr().out
    assert listed(http, http.headers, surface="cli")["choices"] == {"cli": "local:other-model"}
    assert listed(http, http.headers, surface="cli")["current"]["ref"] == "local:other-model"
    command(api, "/model 7")
    assert "not one of the models offered" in capsys.readouterr().out
    command(api, "/model default")
    assert listed(http, http.headers, surface="cli")["choices"] == {}


# --- what a model can do ---------------------------------------------------------------------------------


def test_what_a_provider_says_a_model_can_do_is_kept_only_where_it_is_of_the_right_kind():
    assert Capabilities.from_record({"thinking": True, "tools": False, "vision": "yes", "context": 262144}) == (
        Capabilities(True, False, None, 262144)
    )
    assert Capabilities.from_record({"thinking": 1, "context": True}) == Capabilities()  # neither a yes nor a size
    assert Capabilities.from_record({"context": 0}) == Capabilities()
    assert Capabilities.from_json("not json") == Capabilities()
    assert Capabilities.from_json(Capabilities(True, None, True, 8192).to_json()) == Capabilities(True, None, True, 8192)


async def test_the_catalogue_keeps_what_each_provider_says_so_users_see_it_after_a_restart(settings, memory):
    said = {"fake-big": {"thinking": True, "tools": True, "vision": False, "context": 262144}}

    class Says(FakeBackend):
        async def model_capabilities(self, names):
            return {name: said[name] for name in names if name in said}

    def factory(config, model):
        return Says(model=model) if config.id == "local" else FakeBackend(model=model)

    catalog = ModelCatalog(memory, ProviderManager.from_settings(settings, factory=factory))
    found = {info.ref: info for info in await catalog.listing()}
    assert found["local:fake-big"].capabilities == Capabilities(True, True, False, 262144)
    assert found["local:fake"].capabilities == Capabilities()  # the provider said nothing of it
    catalog.set_enabled(["local:fake-big"], True)
    (usable,) = catalog.usable()
    assert usable.describe()["capabilities"] == {"thinking": True, "tools": True, "vision": False, "context": 262144}

    # a restart, with a provider that does not say: what was said before is still there
    plain = ProviderManager.from_settings(settings, factory=lambda config, model: FakeBackend(model=model))
    again = ModelCatalog(memory, plain)
    assert again.capabilities("local:fake-big")["context"] == 262144


@pytest.fixture
def described_http(settings):
    farm = Farm(capabilities={"local": {"fake-big": {"thinking": True, "tools": True, "vision": True, "context": 131072}}})
    with TestClient(create_app(settings, make_providers(settings, farm))) as client:
        client.app.state.users.create("root", PASSWORD, admin=True)
        client.app.state.users.create("erwan", PASSWORD)
        yield client


def test_users_and_administrators_are_told_what_each_model_can_do(described_http):
    admin, erwan = bearer(described_http, "root"), bearer(described_http)
    described_http.patch("/v1/admin/catalog", json={"refs": ["local:fake-big"], "enabled": True}, headers=admin)
    described_http.get("/v1/admin/catalog", params={"refresh": "true"}, headers=admin)  # asks the providers now
    (model,) = listed(described_http, erwan)["models"]
    assert model["capabilities"] == {"thinking": True, "tools": True, "vision": True, "context": 131072}
    told = described_http.get("/v1/admin/models", headers=admin).json()
    assert told["capabilities"]["fake-big"]["context"] == 131072
    assert told["capabilities"]["other-model"] == {"thinking": None, "tools": None, "vision": None, "context": None}
