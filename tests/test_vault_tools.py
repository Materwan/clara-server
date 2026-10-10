"""The vault_* tools in a conversation: who gets them, what the model can do, how the server opens the vault."""

from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FakeBackend, fake_providers, say
from fastapi.testclient import TestClient

from clara.agent import Agent, ChatRequest
from clara.llm import LlmChunk, ToolCall
from clara.prompt import SystemPrompt
from clara.server import create_app
from clara.settings import Settings
from clara.tools import default_toolbox
from clara.users import Users
from clara.vault import Vault, owner_check
from clara.vaulttools import VAULT_TOOLS, vault_tools

ADMIN = {"Authorization": "Bearer secret-admin"}


def use(tool: str, **arguments) -> list[LlmChunk]:
    return [LlmChunk(tool_calls=[ToolCall(tool, arguments)])]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "_templates").mkdir(parents=True)
    (vault / "_templates" / "idea.md").write_text("---\ntype: idea\nstatus: seed\n---\n\n# {{title}}\n\n## The idea\n")
    return vault


def make_agent(memory, tmp_path, backend, root, allowed=(), **options) -> Agent:
    return Agent(
        memory, backend, default_toolbox(extra=vault_tools()), SystemPrompt(tmp_path / "none.md"),
        vault=Vault(root), vault_allowed=lambda person_id: person_id in allowed, **options,
    )


async def run(agent: Agent, **fields) -> list[dict]:
    request = ChatRequest(**{"surface": "web", "user_id": "erwan", "user_name": "Erwan", "message": "hi", **fields})
    return [event async for event in agent.turn(request)]


def offered(backend: FakeBackend) -> set[str]:
    tools = backend.calls[0][1] or []
    return {schema["function"]["name"] for schema in tools}


def system_prompt(backend: FakeBackend) -> str:
    return backend.calls[0][0][0]["content"]


async def test_the_owner_gets_the_tools_and_the_map_of_the_vault(memory, tmp_path, root):
    erwan = memory.resolve("web", "erwan", "Erwan")
    backend = FakeBackend(say("ok"))
    await run(make_agent(memory, tmp_path, backend, root, allowed={erwan.id}))
    assert VAULT_TOOLS <= offered(backend)
    assert "Your second brain" in system_prompt(backend) and "2-ideas/" in system_prompt(backend)


async def test_somebody_else_gets_neither_the_tools_nor_a_word_about_the_vault(memory, tmp_path, root):
    memory.resolve("web", "erwan", "Erwan")
    backend = FakeBackend(say("ok"))
    await run(make_agent(memory, tmp_path, backend, root, allowed=set()))
    assert not VAULT_TOOLS & offered(backend)
    assert "second brain" not in system_prompt(backend)


async def test_a_tool_called_anyway_by_someone_not_allowed_does_nothing(memory, tmp_path, root):
    backend = FakeBackend(use("vault_create_note", title="Sneaky", type="idea", content="x"), say("ok"))
    events = await run(make_agent(memory, tmp_path, backend, root, allowed=set()))
    assert "not available" in next(e for e in events if e["type"] == "tool")["result"]
    assert not list(root.rglob("Sneaky.md"))


async def test_not_in_a_group_where_everybody_reads_the_answers(memory, tmp_path, root):
    erwan = memory.resolve("web", "erwan", "Erwan")
    backend = FakeBackend(say("ok"))
    await run(make_agent(memory, tmp_path, backend, root, allowed={erwan.id}), space="discord:guild:1")
    assert not VAULT_TOOLS & offered(backend)
    backend = FakeBackend(say("ok"))
    await run(make_agent(memory, tmp_path, backend, root, allowed={erwan.id}, vault_in_groups=True), space="discord:guild:1")
    assert VAULT_TOOLS <= offered(backend)


async def test_without_a_vault_there_are_no_vault_tools(memory, tmp_path):
    backend = FakeBackend(say("ok"))
    await run(Agent(memory, backend, default_toolbox(), SystemPrompt(tmp_path / "none.md")))
    assert not VAULT_TOOLS & offered(backend)


async def test_clara_files_a_thought_then_works_on_it(memory, tmp_path, root):
    erwan = memory.resolve("web", "erwan", "Erwan")
    backend = FakeBackend(
        use("vault_capture", text="Idea: weekly review on Sunday evening", tags="routine, review"),
        use("vault_create_note", title="Weekly review", type="idea", content="Every Sunday: look at the week.", tags=["routine"]),
        use("vault_set_properties", note="Weekly review", set={"status": "growing"}, add_tags="review"),
        use("vault_append_note", note="Weekly review", text="- try it for a month", heading="Next steps"),
        use("vault_search", query="sunday"),
        use("vault_read", note="Weekly review"),
        say("Done."),
    )
    events = await run(make_agent(memory, tmp_path, backend, root, allowed={erwan.id}), timezone="Europe/Paris")
    results = [e["result"] for e in events if e["type"] == "tool"]
    assert results[0].startswith("Created 0-inbox/") and "inbox" in results[0]
    assert results[1].startswith("Created 2-ideas/Weekly review.md")
    assert results[2] == "Updated status, tags in 2-ideas/Weekly review.md."
    assert results[3].startswith("Added to 2-ideas/Weekly review.md under 'Next steps'")
    assert "Weekly review.md" in results[4] and "Sunday" in results[4]
    assert "idea/growing" in results[5] and "- try it for a month" in results[5]
    assert (root / "2-ideas/Weekly review.md").read_text().count("review") >= 2


async def test_a_mistake_comes_back_as_text_the_model_can_act_on(memory, tmp_path, root):
    erwan = memory.resolve("web", "erwan", "Erwan")
    backend = FakeBackend(
        use("vault_read", note="Nothing here"),
        use("vault_create_note", title="X", type="bogus"),
        use("vault_create_note", title="Y", type="idea", properties="not json"),
        use("vault_query", where=["not", "a", "dict"]),
        use("vault_delete_note", note="../../etc/passwd"),
        say("sorry"),
    )
    events = await run(make_agent(memory, tmp_path, backend, root, allowed={erwan.id}))
    results = [e["result"] for e in events if e["type"] == "tool"]
    assert results[0].startswith("Error: No note 'Nothing here'")
    assert results[1].startswith("Error: Unknown note type 'bogus'")
    assert results[2].startswith("Error: properties must be an object")
    assert results[3].startswith("Error: where must be an object")
    assert results[4].startswith("Error:")
    assert "failed" not in " ".join(results)


async def test_loose_argument_types_from_the_model_are_accepted(memory, tmp_path, root):
    erwan = memory.resolve("web", "erwan", "Erwan")
    backend = FakeBackend(
        use("vault_create_note", title="Loose", type="idea", content="x", tags="a, b", properties='{"priority": 2}'),
        use("vault_list", limit="5", recursive="false", folder="2-ideas"),
        use("vault_query", tags="a", where='{"priority": ">=2"}', limit="3"),
        say("ok"),
    )
    events = await run(make_agent(memory, tmp_path, backend, root, allowed={erwan.id}))
    results = [e["result"] for e in events if e["type"] == "tool"]
    assert results[0].startswith("Created") and "Loose.md" in results[1] and "Loose.md" in results[2]


# --- who owns the vault ------------------------------------------------------------------------------------


def test_the_owners_are_the_named_users_or_else_the_administrators(memory, settings):
    users = Users(memory, session_days=90)
    boss = users.create("boss", "correct horse battery", admin=True)
    friend = users.create("friend", "correct horse battery")
    stranger = memory.resolve("discord", "999", "Stranger")
    admins_only = owner_check(settings, users)
    assert admins_only(boss.person_id) and not admins_only(friend.person_id) and not admins_only(stranger.id)
    named = owner_check(replace(settings, vault_owners=frozenset({"friend"})), users)
    assert named(friend.person_id) and not named(boss.person_id)
    users.create("spare", "correct horse battery", admin=True)
    users.set_disabled("boss", True)
    assert not admins_only(boss.person_id)


def test_the_settings_read_the_vault_variables(settings, tmp_path):
    env = {
        "CLARA_TOKENS": "t:abcdefghijklmnopqrstuvwxyz0123456789", "CLARA_DATA_DIR": str(tmp_path),
        "CLARA_VAULT_PATH": "~/second-brain", "CLARA_VAULT_OWNERS": "Erwan, clara-friend", "CLARA_VAULT_IN_GROUPS": "true",
        "CLARA_VAULT_PUSH": "no", "CLARA_VAULT_EMBED_MODEL": "off",
    }
    parsed = Settings.from_env(env)
    assert parsed.vault_path == Path("~/second-brain").expanduser()
    assert parsed.vault_owners == {"erwan", "clara-friend"} and parsed.vault_in_groups and not parsed.vault_push
    assert settings.vault_path is None and not settings.vault_in_groups


# --- the server --------------------------------------------------------------------------------------------


def test_the_server_opens_the_vault_and_the_operator_console_manages_it(settings, root):
    configured = replace(settings, vault_path=root, vault_embed_model="off")
    with TestClient(create_app(configured, fake_providers(configured, FakeBackend()))) as client:
        assert client.app.state.vault is not None
        run_command = lambda line: client.post("/v1/admin/command", json={"line": line}, headers=ADMIN).json()["output"]  # noqa: E731
        assert "vault is empty" in run_command("/vault")
        assert "administrators may use the vault" in run_command("/vault owners")
        assert "not a git repository" in run_command("/vault sync")
        assert "Semantic search is not set up" in run_command("/vault reindex")  # reported, not a crash
        assert "good shape" in run_command("/vault health")
        assert run_command("/vault bogus").startswith("!")


def test_without_a_vault_path_the_server_has_none(settings):
    with TestClient(create_app(settings, fake_providers(settings, FakeBackend()))) as client:
        assert client.app.state.vault is None
        output = client.post("/v1/admin/command", json={"line": "/vault"}, headers=ADMIN).json()["output"]
        assert "CLARA_VAULT_PATH" in output


def test_a_missing_vault_folder_does_not_stop_the_server(settings, tmp_path):
    configured = replace(settings, vault_path=tmp_path / "nowhere")
    with TestClient(create_app(configured, fake_providers(configured, FakeBackend()))) as client:
        assert client.app.state.vault is None
