import time
from dataclasses import replace

import pytest
from conftest import FakeBackend, fake_providers

from clara.agent import Agent
from clara.commands import CommandContext, registry
from clara.prompt import SystemPrompt
from clara.tools import default_toolbox


@pytest.fixture
def ctx(settings, memory, tmp_path) -> CommandContext:
    providers = fake_providers(settings)
    agent = Agent(memory, providers, default_toolbox(), SystemPrompt(tmp_path / "none.md"))
    return CommandContext(settings, memory, agent, providers, time.monotonic(), "127.0.0.1:8765")


async def run(ctx: CommandContext, line: str) -> str:
    return (await registry.execute(line, ctx)).output


async def test_help_lists_every_command_and_describes_one(ctx):
    listing = await run(ctx, "/help")
    for name in registry.names:
        assert f"/{name}" in listing
    assert "switch where the model runs" in await run(ctx, "/help provider")
    assert (await run(ctx, "/help nope")).startswith("!")


async def test_unknown_commands_and_the_optional_slash(ctx):
    assert "Unknown command" in await run(ctx, "/nope")
    assert await run(ctx, "") == ""
    assert await run(ctx, "people") == "Nobody yet."  # works without the slash


async def test_quit(ctx):
    assert (await registry.execute("/quit", ctx)).quit


async def test_provider_lists_then_switches_and_persists(ctx, settings):
    listing = await run(ctx, "/provider")
    assert "Local host" in listing and "Ollama API key" in listing and "(key set)" in listing
    local_line = next(line for line in listing.splitlines() if " local " in line)
    assert local_line.lstrip().startswith("*")

    answer = await run(ctx, "/provider cloud")
    assert "Now using cloud (Ollama API key), model fake-big" in answer
    assert "Reachable." in answer
    assert ctx.providers.active == "cloud"
    assert "key-123" not in listing + answer
    assert '"cloud"' in settings.runtime_state_file.read_text(encoding="utf-8")


async def test_provider_without_key_is_refused(ctx):
    ctx.providers.configs["cloud"] = replace(ctx.providers.configs["cloud"], api_key=None)
    listing = await run(ctx, "/provider")
    assert "(no OLLAMA_API_KEY)" in listing
    answer = await run(ctx, "/provider cloud")
    assert answer.startswith("!") and "OLLAMA_API_KEY" in answer
    assert ctx.providers.active == "local"


async def test_provider_reports_when_unreachable_but_still_switches(ctx, settings):
    class Down(FakeBackend):
        async def list_models(self):
            raise ConnectionError("connection refused")

    ctx.providers._factory = lambda config, model: Down(model=model)
    answer = await run(ctx, "/provider cloud")
    assert "Now using cloud" in answer and "Not reachable" in answer and "refused" in answer
    assert ctx.providers.active == "cloud"


async def test_model_shows_validates_and_sets(ctx):
    shown = await run(ctx, "/model")
    assert "* fake" in shown and "other-model" in shown

    assert (await run(ctx, "/model nonexistent")).startswith("!")
    assert ctx.providers.model == "fake"

    assert "now uses other-model" in await run(ctx, "/model other-model")
    assert ctx.providers.model == "other-model"


async def test_model_still_changes_when_the_provider_is_unreachable(ctx):
    class Down(FakeBackend):
        async def list_models(self):
            raise ConnectionError("down")

    ctx.providers._factory = lambda config, model: Down(model=model)
    ctx.providers.set_model("fake")  # rebuild with the failing backend
    answer = await run(ctx, "/model something")
    assert "now uses something" in answer and "Not checked" in answer


async def test_status(ctx):
    ctx.memory.resolve("cli", "erwan", "Erwan")
    status = await run(ctx, "/status")
    assert "local (Local host)" in status
    assert "fake" in status and "1 people" in status and "127.0.0.1:8765" in status


async def test_people_and_facts_management(ctx):
    erwan = ctx.memory.resolve("cli", "erwan", "Erwan")
    ctx.memory.resolve("discord", "1234", "Zoe")

    people = await run(ctx, "/people")
    assert "Erwan" in people and "cli:erwan" in people and "discord:1234" in people

    # by name, by id and by account
    assert "Stored." == await run(ctx, "/facts Erwan add Likes jazz")
    assert "Already known." == await run(ctx, f"/facts {erwan.id} add likes jazz")
    listed = await run(ctx, "/facts cli:erwan")
    assert "Likes jazz" in listed

    fact_id = ctx.memory.facts(erwan.id)[0].id
    assert await run(ctx, f"/facts erwan del {fact_id}") == "Deleted."
    assert "(no facts)" in await run(ctx, "/facts erwan")
    assert (await run(ctx, f"/facts erwan del {fact_id}")).startswith("!")


async def test_facts_errors(ctx):
    assert (await run(ctx, "/facts")).startswith("!")
    assert "Nobody matches" in await run(ctx, "/facts ghost")
    ctx.memory.resolve("cli", "a", "Sam")
    ctx.memory.resolve("cli", "b", "Sam")
    assert "people are called sam" in await run(ctx, "/facts sam")
    assert (await run(ctx, "/facts sam add " + "x" * 400)).startswith("!")  # ambiguous first
    assert (await run(ctx, "/facts 1 add " + "x" * 400)).startswith("!")  # too long
    assert (await run(ctx, "/facts 1 wat")).startswith("!")


async def test_link_merges_accounts(ctx):
    erwan = ctx.memory.resolve("cli", "erwan", "Erwan")
    other = ctx.memory.resolve("discord", "1234", "erwan#0001")
    ctx.memory.add_fact(other.id, "Plays guitar")

    answer = await run(ctx, "/link discord:1234 Erwan")
    assert "cli:erwan, discord:1234" in answer
    assert ctx.memory.find_person("discord", "1234") == erwan
    assert [fact.text for fact in ctx.memory.facts(erwan.id)] == ["Plays guitar"]


async def test_link_new_account_and_bad_usage(ctx):
    erwan = ctx.memory.resolve("cli", "erwan", "Erwan")
    await run(ctx, "/link web:42 Erwan")
    assert ctx.memory.find_person("web", "42") == erwan
    assert (await run(ctx, "/link nonsense Erwan")).startswith("!")
    assert (await run(ctx, "/link Bad Surface:1 Erwan")).startswith("!")
    assert (await run(ctx, "/link web:43 ghost")).startswith("!")


async def test_a_crashing_command_does_not_kill_the_console(ctx, monkeypatch):
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(ctx.memory, "summaries", boom)
    assert (await run(ctx, "/people")).startswith("! The command failed")


async def test_describe_gives_completion_choices(ctx):
    by_name = {entry["name"]: entry for entry in registry.describe(ctx)}
    assert by_name["provider"]["choices"] == ["local", "cloud", "gemini", "deepseek", "mistral"]
    assert "status" in by_name["help"]["choices"]
