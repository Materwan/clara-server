import json

import httpx
import pytest
from conftest import FakeBackend, fake_providers

from clara.llm import OllamaBackend, OpenAIBackend, OpenAIFlavor, ollama_record
from clara.providers import ProviderError, ProviderManager, configs_from_settings, default_factory, flavor_of
from clara.settings import Settings, SettingsError


def settings_without_key(settings: Settings) -> Settings:
    from dataclasses import replace

    return replace(settings, ollama_api_key=None)


def test_starts_on_the_default_provider(settings):
    providers = fake_providers(settings)
    assert providers.active == "local"
    assert providers.model == "fake"
    assert providers.model_of("cloud") == "fake-big"


@pytest.mark.parametrize("name", ["cloud", "CLOUD", "apikey", "api-key", "ollama-api-key"])
def test_switch_accepts_aliases(settings, name):
    providers = fake_providers(settings)
    assert providers.switch(name).id == "cloud"
    assert providers.active == "cloud"
    assert providers.switch("localhost").id == "local"


def test_unknown_provider_is_refused(settings):
    with pytest.raises(ProviderError):
        fake_providers(settings).switch("openai")


def test_cloud_needs_an_api_key(settings):
    providers = fake_providers(settings_without_key(settings))
    with pytest.raises(ProviderError, match="OLLAMA_API_KEY"):
        providers.switch("cloud")
    assert providers.active == "local"


def test_switching_changes_the_backend_used_for_chat(settings):
    local, cloud = FakeBackend(model="local-fake"), FakeBackend(model="cloud-fake")
    by_host = {configs_from_settings(settings)["local"].host: local}
    providers = ProviderManager.from_settings(
        settings, factory=lambda config, model: by_host.get(config.host, cloud)
    )
    assert providers._backend is local
    providers.switch("cloud")
    assert providers._backend is cloud


def test_choice_and_models_survive_a_restart(settings):
    providers = fake_providers(settings)
    providers.switch("cloud")
    providers.set_model("other-model")

    again = fake_providers(settings)
    assert again.active == "cloud"
    assert again.model == "other-model"
    assert again.model_of("local") == "fake"


def test_saved_state_never_contains_the_api_key(settings):
    fake_providers(settings).switch("cloud")
    saved = settings.runtime_state_file.read_text(encoding="utf-8")
    assert "key-123" not in saved
    assert json.loads(saved)["provider"] == "cloud"


def test_saved_cloud_choice_is_ignored_when_the_key_is_gone(settings):
    fake_providers(settings).switch("cloud")
    assert fake_providers(settings_without_key(settings)).active == "local"


def test_corrupt_state_file_is_ignored(settings):
    settings.data_dir.mkdir(parents=True)
    settings.runtime_state_file.write_text("{not json", encoding="utf-8")
    assert fake_providers(settings).active == "local"


async def test_check_reports_unreachable_providers(settings):
    class Down(FakeBackend):
        async def list_models(self):
            raise ConnectionError("refused")

    providers = fake_providers(settings, Down())
    assert "refused" in await providers.check()
    assert await fake_providers(settings).check() is None


def test_cloud_backend_sends_the_api_key_and_local_does_not(settings):
    configs = configs_from_settings(settings)
    cloud = default_factory(configs["cloud"], "gpt-oss:120b")
    local = default_factory(configs["local"], "llama3")
    assert isinstance(cloud, OllamaBackend)
    assert cloud._client._client.headers["authorization"] == "Bearer key-123"
    assert "authorization" not in local._client._client.headers
    assert str(cloud._client._client.base_url).startswith("https://ollama.com")


def test_secrets_are_not_in_the_settings_repr(settings):
    shown = repr(settings)
    assert "key-123" not in shown and "secret-cli" not in shown and "secret-admin" not in shown


def test_settings_cloud_default_needs_a_key():
    with pytest.raises(SettingsError):
        Settings.from_env({"CLARA_TOKENS": "a:b", "CLARA_PROVIDER": "cloud"})


def test_settings_reject_a_token_used_for_chat_and_admin():
    with pytest.raises(SettingsError):
        Settings.from_env({"CLARA_TOKENS": "a:same", "CLARA_ADMIN_TOKENS": "b:same"})


async def test_api_key_provider_rejects_a_bad_key(settings, monkeypatch):
    import httpx

    from clara.llm import OllamaBackend

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/me"
        ok = request.headers["authorization"] == "Bearer good"
        return httpx.Response(200 if ok else 401, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    class Listing:  # stands in for the ollama client's /api/tags
        async def list(self):
            from types import SimpleNamespace as NS

            return NS(models=[NS(model="gpt-oss:120b")])

    bad = OllamaBackend("m", host="https://ollama.com", api_key="bad", client=Listing())
    good = OllamaBackend("m", host="https://ollama.com", api_key="good", client=Listing())
    with pytest.raises(PermissionError, match="rejected"):
        await bad.verify()
    await good.verify()


# --- what each provider says a model can do --------------------------------------------------------------


def test_ollama_says_what_a_model_can_do_and_an_older_one_only_its_context():
    assert ollama_record(["completion", "tools", "thinking", "vision"], {"qwen35.context_length": 262144}) == {
        "thinking": True, "tools": True, "vision": True, "context": 262144,
    }
    assert ollama_record(["completion"], {"phi3.context_length": 131072, "general.name": "phi3"}) == {
        "thinking": False, "tools": False, "vision": False, "context": 131072,
    }
    assert ollama_record(None, {"llama.context_length": 8192}) == {"context": 8192}


async def test_ollama_is_asked_about_each_model_and_one_that_does_not_answer_is_left_out():
    from types import SimpleNamespace as NS

    class Shows:  # stands in for the ollama client's /api/show
        async def show(self, name):
            if name == "broken:latest":
                raise ConnectionError("no answer")
            if name == "old:latest":
                return NS(capabilities=None, modelinfo={"old.context_length": 4096})
            return NS(capabilities=["completion", "tools"], modelinfo={"qwen.context_length": 262144})

    backend = OllamaBackend("m", client=Shows())
    assert await backend.model_capabilities(["qwen3:8b", "broken:latest", "old:latest"]) == {
        "qwen3:8b": {"thinking": False, "tools": True, "vision": False, "context": 262144},
        "old:latest": {"context": 4096},
    }


async def test_gemini_says_what_its_models_can_do_from_its_own_list_over_every_page(settings):
    pages = {
        None: {"models": [{"name": "models/gemini-2.5-flash", "thinking": True, "inputTokenLimit": 1048576,
                           "supportedGenerationMethods": ["generateContent", "countTokens"]}],
               "nextPageToken": "2"},
        "2": {"models": [{"name": "models/gemini-2.5-flash-preview-tts", "inputTokenLimit": 8192,
                          "supportedGenerationMethods": ["generateContent"]},
                         {"name": "models/gemini-embedding-001", "inputTokenLimit": 2048,
                          "supportedGenerationMethods": ["embedContent"]}]},
    }
    asked = []

    def handler(request):
        assert request.url.path == "/v1beta/models"
        assert request.headers["x-goog-api-key"] == "key-123"
        assert "authorization" not in request.headers  # the Bearer of the OpenAI API is refused by this one
        token = request.url.params.get("pageToken")
        asked.append(token)
        return httpx.Response(200, json=pages[token])

    backend = OpenAIBackend(
        "gemini-2.5-flash", host="https://generativelanguage.googleapis.com/v1beta/openai", api_key="key-123",
        flavor=flavor_of("gemini", settings), label="Google Gemini", transport=httpx.MockTransport(handler),
    )
    found = await backend.model_capabilities(["gemini-2.5-flash", "gemini-2.5-flash-preview-tts", "gemini-embedding-001", "gemini-gone"])
    assert found == {
        "gemini-2.5-flash": {"thinking": True, "tools": True, "vision": True, "context": 1048576},
        "gemini-2.5-flash-preview-tts": {"thinking": False, "tools": None, "vision": None, "context": 8192},
        "gemini-embedding-001": {"thinking": False, "tools": None, "vision": None, "context": 2048},
    }
    assert asked == [None, "2"]


async def test_a_service_without_a_list_of_its_own_says_nothing_more_than_its_names(settings):
    deepseek = OpenAIBackend("deepseek-chat", host="https://api.deepseek.com", api_key="key-123", flavor=OpenAIFlavor())
    assert await deepseek.model_capabilities(["deepseek-chat"]) == {}
