"""Gemini, DeepSeek and Mistral: the OpenAI-compatible backend, and their place among the providers."""

import json
from dataclasses import replace

import httpx
import pytest
from conftest import FakeBackend, untimed

from clara.agent import Agent
from clara.llm import SKIP_SIGNATURE, LlmChunk, LlmError, OpenAIBackend, OpenAIFlavor, ToolCall
from clara.prompt import SystemPrompt
from clara.providers import ProviderError, ProviderManager, configs_from_settings, default_factory
from clara.settings import Settings, SettingsError
from clara.tools import default_toolbox


def sse(*parts: dict) -> bytes:
    return "".join(f"data: {json.dumps(part)}\n\n" for part in parts).encode() + b"data: [DONE]\n\n"


class Recorder:
    """An httpx transport that answers from a list and keeps the requests."""

    def __init__(self, *answers: httpx.Response):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answers.pop(0)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


def backend(recorder: Recorder, flavor: OpenAIFlavor | None = None) -> OpenAIBackend:
    return OpenAIBackend("model-x", "https://api.example.com/v1/", "sk-test", flavor, "Example", recorder.transport)


async def collect(stream) -> list[LlmChunk]:
    return [chunk async for chunk in stream]


HISTORY = [
    {"role": "system", "content": "be nice"},
    {"role": "user", "content": "list my files"},
    {
        "role": "assistant",
        "content": "",
        "thinking": "I should look.",
        "tool_calls": [
            {"function": {"name": "ls", "arguments": {"path": "."}}, "extra_content": {"google": {"thought_signature": "sig1"}}},
            {"function": {"name": "pwd", "arguments": {}}},
        ],
    },
    {"role": "tool", "tool_name": "ls", "content": "a.txt"},
    {"role": "tool", "tool_name": "pwd", "content": "/home"},
    {"role": "assistant", "content": "You have a.txt."},
]


# --- converting the messages -------------------------------------------------------------------------


def test_tool_calls_get_ids_and_their_results_point_to_them():
    converted = backend(Recorder()).convert(HISTORY)
    calls = converted[2]["tool_calls"]
    assert [c["id"] for c in calls] == ["c00000001", "c00000002"]
    assert all(len(c["id"]) == 9 and c["id"].isalnum() for c in calls)  # what Mistral demands
    assert calls[0]["function"] == {"name": "ls", "arguments": '{"path": "."}'}
    assert converted[2]["content"] is None
    assert [m["tool_call_id"] for m in converted[3:5]] == ["c00000001", "c00000002"]
    assert "name" not in converted[3] and "reasoning_content" not in converted[2]
    assert "extra_content" not in calls[0]  # only Gemini gets the signatures


def test_gemini_gets_its_signatures_back_and_a_stand_in_for_calls_without_one():
    calls = backend(Recorder(), OpenAIFlavor(signatures=True)).convert(HISTORY)[2]["tool_calls"]
    assert calls[0]["extra_content"] == {"google": {"thought_signature": "sig1"}}
    assert calls[1]["extra_content"] == {"google": {"thought_signature": SKIP_SIGNATURE}}


def test_deepseek_gets_the_reasoning_back_with_the_calls():
    converted = backend(Recorder(), OpenAIFlavor(reasoning_back=True)).convert(HISTORY)
    assert converted[2]["reasoning_content"] == "I should look."
    assert "reasoning_content" not in converted[5]  # an answer without calls does not need it


def test_mistral_names_the_tool_of_a_result():
    converted = backend(Recorder(), OpenAIFlavor(tool_names=True)).convert(HISTORY)
    assert converted[3]["name"] == "ls"


# --- streaming --------------------------------------------------------------------------------------


async def test_streams_text_reasoning_tool_calls_and_usage():
    recorder = Recorder(httpx.Response(200, content=sse(
        {"choices": [{"delta": {"reasoning_content": "Hmm. "}}]},
        {"choices": [{"delta": {"content": "Let me "}}]},
        {"choices": [{"delta": {"content": "look."}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "x", "function": {"name": "read_", "arguments": '{"pa'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "file", "arguments": 'th": "a.py"}'},
                                                "extra_content": {"google": {"thought_signature": "s"}}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 12, "completion_tokens": 5}},
    )))
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}}}]
    chunks = await collect(backend(recorder).stream([{"role": "user", "content": "hi"}], tools))
    assert "".join(c.text for c in chunks) == "Let me look."
    assert "".join(c.thinking for c in chunks) == "Hmm. "
    assert sum(c.prompt_tokens for c in chunks) == 12 and sum(c.completion_tokens for c in chunks) == 5
    assert chunks[-1].tool_calls == [ToolCall("read_file", {"path": "a.py"}, {"google": {"thought_signature": "s"}})]
    request = recorder.requests[0]
    assert str(request.url) == "https://api.example.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    body = recorder.body()
    assert body["model"] == "model-x" and body["stream"] is True and body["tools"] == tools
    assert body["stream_options"] == {"include_usage": True}


async def test_extra_fields_and_no_usage_request_when_the_flavor_says_so():
    recorder = Recorder(httpx.Response(200, content=sse({"choices": [{"delta": {"content": "ok"}}]})))
    flavor = OpenAIFlavor(stream_usage=False, extra_body=(("thinking", {"type": "disabled"}),))
    await collect(backend(recorder, flavor).stream([{"role": "user", "content": "hi"}], None))
    body = recorder.body()
    assert "stream_options" not in body and "tools" not in body
    assert body["thinking"] == {"type": "disabled"}


async def test_an_error_answer_says_why():
    recorder = Recorder(httpx.Response(400, json={"error": {"message": "model not found"}}))
    with pytest.raises(LlmError, match="Example answered HTTP 400: model not found"):
        await collect(backend(recorder).stream([{"role": "user", "content": "hi"}], None))


async def test_gemini_list_of_errors_is_read_too():
    recorder = Recorder(httpx.Response(400, json=[{"error": {"code": 400, "message": "bad schema"}}]))
    with pytest.raises(LlmError, match="bad schema"):
        await collect(backend(recorder).stream([{"role": "user", "content": "hi"}], None))


async def test_models_are_listed_without_googles_prefix():
    recorder = Recorder(httpx.Response(200, json={"data": [{"id": "models/gemini-a"}, {"id": "b"}, {"id": "b"}]}))
    assert await backend(recorder).list_models() == ["b", "gemini-a"]
    assert str(recorder.requests[0].url) == "https://api.example.com/v1/models"


async def test_a_refused_key_fails_the_check():
    recorder = Recorder(httpx.Response(401, json={"error": {"message": "invalid key"}}))
    with pytest.raises(PermissionError, match="refused the API key"):
        await backend(recorder).verify()


# --- the providers ----------------------------------------------------------------------------------


def with_keys(settings: Settings, **keys: str) -> Settings:
    providers = {
        name: replace(api, api_key=keys.get(name)) for name, api in settings.api_providers.items()
    }
    return replace(settings, api_providers=providers)


def test_the_new_providers_are_listed_but_need_their_key(settings):
    configs = configs_from_settings(settings)
    assert [c.id for c in configs.values()] == ["local", "cloud", "gemini", "deepseek", "mistral"]
    assert [c.usable for c in configs.values()][2:] == [False, False, False]
    providers = ProviderManager.from_settings(settings, factory=lambda config, model: FakeBackend(model=model))
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        providers.switch("google")
    assert providers.active == "local"


def test_with_a_key_a_provider_can_be_chosen_and_speaks_the_openai_api(settings):
    settings = with_keys(settings, gemini="g-key", deepseek="d-key", mistral="m-key")
    configs = configs_from_settings(settings)
    gemini = configs["gemini"]
    assert (gemini.label, gemini.default_model, gemini.context_window) == ("Google Gemini", "gemini-flash-latest", 1_048_576)
    assert gemini.host == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert configs["deepseek"].default_model == "deepseek-flash" and configs["mistral"].default_model == "mistral-large-latest"
    made = default_factory(gemini, "gemini-x")
    assert isinstance(made, OpenAIBackend) and made.model == "gemini-x"
    providers = ProviderManager.from_settings(settings, factory=lambda config, model: FakeBackend(model=model))
    assert providers.switch("deepseek").id == "deepseek"
    assert providers.peer == "deepseek" and providers.context_window == 1_000_000


def test_settings_read_the_keys_hosts_models_and_windows(tmp_path):
    settings = Settings.from_env({
        "CLARA_TOKENS": "terminal:secret-cli",
        "CLARA_DATA_DIR": str(tmp_path),
        "MISTRAL_API_KEY": "mistral-secret-key",
        "CLARA_MISTRAL_MODEL": "mistral-small-latest",
        "CLARA_MISTRAL_CONTEXT_WINDOW": "32000",
        "CLARA_PROVIDER": "mistral",
        "CLARA_DEEPSEEK_THINKING": "false",
    })
    mistral = settings.api_providers["mistral"]
    assert (mistral.api_key, mistral.model, mistral.context_window) == ("mistral-secret-key", "mistral-small-latest", 32000)
    assert settings.default_provider == "mistral" and not settings.deepseek_thinking
    assert dict(configs_from_settings(settings)["deepseek"].flavor.extra_body) == {"thinking": {"type": "disabled"}}
    assert "mistral-secret-key" not in repr(settings)  # the keys are never shown


def test_choosing_a_provider_without_its_key_at_start_is_refused(tmp_path):
    with pytest.raises(SettingsError, match="DEEPSEEK_API_KEY"):
        Settings.from_env({"CLARA_TOKENS": "t:secret", "CLARA_DATA_DIR": str(tmp_path), "CLARA_PROVIDER": "deepseek"})


# --- what the agent keeps of a round of tool calls ------------------------------------------------------


async def test_reasoning_and_signatures_go_back_with_the_calls_now_and_in_later_turns(memory, tmp_path):
    fake = FakeBackend(
        [LlmChunk(thinking="Look first."), LlmChunk(tool_calls=[ToolCall("recall_facts", {"query": "x"}, {"sig": "1"})])],
        [LlmChunk(text="Nothing known.")],
        [LlmChunk(text="Still nothing.")],
    )
    agent = Agent(memory, fake, default_toolbox(), SystemPrompt(tmp_path / "none.md"))
    from clara.agent import ChatRequest

    async for _ in agent.turn(ChatRequest("cli", "u", "U", "what do you know?")):
        pass
    sent = fake.calls[1][0][-2]  # the second round: the assistant's calls, then the result
    assert sent["thinking"] == "Look first."
    assert sent["tool_calls"][0]["extra_content"] == {"sig": "1"}
    async for _ in agent.turn(ChatRequest("cli", "u", "U", "and now?")):
        pass
    replayed = [m for m in fake.calls[2][0] if m.get("tool_calls")]
    assert replayed[0]["thinking"] == "Look first."
    assert replayed[0]["tool_calls"][0]["extra_content"] == {"sig": "1"}
    assert untimed(fake.calls[2][0][-1]["content"]) == "and now?"


async def test_one_connection_pool_serves_every_request_and_is_closed_at_the_end():
    models = httpx.Response(200, json={"data": [{"id": "a"}]})
    recorder = Recorder(models, httpx.Response(200, json={"data": [{"id": "a"}]}))
    made = backend(recorder)
    await made.list_models()
    first = made._http.get()
    await made.list_models()
    assert made._http.get() is first and len(recorder.requests) == 2
    await made.aclose()
    assert first.is_closed
