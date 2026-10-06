"""Configuration, read once from the environment (and a `.env` file)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

from .limits import parse_limit
from .tailscale import HTTPS_PORTS
from .tailscale import MODES as TAILSCALE_MODES


class SettingsError(Exception):
    pass


PLACEHOLDER_TOKEN = "change-me"  # what .env.example ships with
MIN_PUBLIC_TOKEN_LENGTH = 32  # `secrets.token_urlsafe(32)` gives 43; a Funnel URL is open to the whole internet
SURFACE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def parse_tokens(raw: str) -> dict[str, str]:
    """`"terminal:abc,discord:def"` -> `{"abc": "terminal", "def": "discord"}`.

    A bare token (no `name:`) is accepted and named "client".
    """
    tokens: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, separator, token = item.partition(":")
        if not separator:
            name, token = "client", item
        name, token = name.strip(), token.strip()
        if not token:
            raise SettingsError(f"Empty token for client {name!r} in CLARA_TOKENS")
        tokens[token] = name
    return tokens


def parse_client_surfaces(raw: str, clients: set[str]) -> dict[str, frozenset[str]]:
    """`"terminal=cli|console,discord=discord"` -> the surfaces each client may speak for."""
    allowed: dict[str, frozenset[str]] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, separator, surfaces = item.partition("=")
        name = name.strip()
        if not separator or not name:
            raise SettingsError(f"CLARA_CLIENT_SURFACES: expected name=surface|surface, got {item!r}")
        if name not in clients:
            raise SettingsError(f"CLARA_CLIENT_SURFACES: {name!r} is not a client of CLARA_TOKENS")
        names = frozenset(part.strip() for part in surfaces.split("|") if part.strip())
        if not names:
            raise SettingsError(f"CLARA_CLIENT_SURFACES: no surface for {name!r}")
        for surface in names:
            if not SURFACE_RE.match(surface):
                raise SettingsError(f"CLARA_CLIENT_SURFACES: bad surface {surface!r} (a-z, 0-9, _ -)")
        allowed[name] = names | allowed.get(name, frozenset())
    return allowed


def parse_user_surfaces(raw: str) -> frozenset[str]:
    """`"web,app"` -> the surfaces a user may log in on."""
    surfaces = frozenset(part.strip().lower() for part in raw.replace("|", ",").split(",") if part.strip())
    if not surfaces:
        raise SettingsError("CLARA_USER_SURFACES is empty: no user could log in anywhere")
    for surface in surfaces:
        if not SURFACE_RE.match(surface):
            raise SettingsError(f"CLARA_USER_SURFACES: bad surface {surface!r} (a-z, 0-9, _ -)")
    return surfaces


def parse_login_surfaces(raw: str) -> frozenset[str]:
    """`"discord"` -> the surfaces where an account must be signed in to talk; `none`: nowhere."""
    if raw.strip().lower() == "none":
        return frozenset()
    surfaces = frozenset(part.strip().lower() for part in raw.replace("|", ",").split(",") if part.strip())
    for surface in surfaces:
        if not SURFACE_RE.match(surface):
            raise SettingsError(f"CLARA_LOGIN_SURFACES: bad surface {surface!r} (a-z, 0-9, _ -)")
    return surfaces


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{key} must be an integer, got {raw!r}") from None
    if value < 1:
        raise SettingsError(f"{key} must be at least 1")
    return value


def _token_limit(env: Mapping[str, str], key: str) -> int:
    """A number of tokens a day (`500000`, `500k`, `2m`); empty, 0 or `off`: no limit."""
    raw = env.get(key, "").strip()
    if not raw:
        return 0
    try:
        return parse_limit(raw)
    except ValueError as error:
        raise SettingsError(f"{key}: {error}") from None


DEFAULT_LOCAL_HOST = "http://localhost:11434"
DEFAULT_CLOUD_HOST = "https://ollama.com"
PROVIDER_IDS = ("local", "cloud", "gemini", "deepseek", "mistral")
# The services reached through their OpenAI-compatible API: id -> (key variable, host, model, context window)
API_PROVIDERS = {
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-flash-latest", 1_048_576),
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-flash", 1_000_000),
    "mistral": ("MISTRAL_API_KEY", "https://api.mistral.ai/v1", "mistral-large-latest", 131_072),
}


@dataclass(frozen=True)
class ApiProvider:
    """One of API_PROVIDERS as configured: CLARA_<ID>_HOST, CLARA_<ID>_MODEL, CLARA_<ID>_CONTEXT_WINDOW."""

    key_name: str
    host: str
    model: str
    context_window: int
    api_key: str | None = field(default=None, repr=False)


def _positive_number(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{key} must be a number, got {raw!r}") from None
    if not 0 < value < 1e6:
        raise SettingsError(f"{key} must be greater than 0")
    return value


def _flag(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise SettingsError(f"{key} must be true or false, got {raw!r}")


def _non_negative_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{key} must be an integer, got {raw!r}") from None
    if value < 0:
        raise SettingsError(f"{key} cannot be negative")
    return value


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    tokens: dict[str, str] = field(repr=False)  # chat token -> client name
    admin_tokens: dict[str, str] = field(repr=False)  # console token -> admin name
    # client name -> surfaces it may speak for; a client with no entry may use any
    client_surfaces: dict[str, frozenset[str]]
    history_turns: int
    max_concurrent_llm: int
    max_tool_rounds: int
    tool_timeout: int  # seconds a client may take to run its tools (it may ask the user first)
    llm_first_token_timeout: int  # seconds the model may take to start answering
    llm_idle_timeout: int  # seconds the model may pause between two pieces of its answer
    llm_retries: int  # times a busy or unreachable model is asked again (0 = never)
    llm_retry_delay: int  # seconds before the first new try; doubled each time
    compact_percent: int  # summarise a conversation when its context is this full (0 = never)
    keep_recent_turns: int  # turns a compaction leaves unsummarised
    facts_token_budget: int  # tokens of remembered facts shown to the model in each prompt
    purge_summarised: bool  # delete messages once a summary stands for them
    reminder_ai_timeout: int  # seconds Clara has to write the announcement of a reminder (0: announce the text)
    system_prompt_file: Path
    # Language model providers (see providers.py)
    default_provider: str
    local_host: str
    local_model: str
    local_context_window: int
    cloud_host: str
    cloud_model: str
    cloud_context_window: int
    ollama_api_key: str | None = field(repr=False)
    web_tools: bool = True  # web_search and web_fetch (they need ollama_api_key)
    notify_long_turn: int = 120  # a turn this long (seconds) notifies its person when done (0: never)
    # The traffic log (traffic.py): every request in and out, in data/logs
    traffic_log: bool = True
    traffic_log_days: int = 30  # files older than this are deleted
    traffic_log_max_body: int = 100_000  # characters kept of each body
    # Tailscale (tailscale.py): off | serve (your tailnet) | funnel (the internet), on an HTTPS port
    tailscale: str = "off"
    tailscale_port: int = 443
    tailscale_bin: str = "tailscale"
    # An address that sends this many bad tokens in a minute is refused for the next block (0: never)
    auth_max_failures: int = 10
    auth_block_seconds: int = 300
    # Users who log in with a password (users.py): days a token lasts without being used (0: for ever), and
    # the surfaces a login may be made on (a user token is bound to one of them)
    session_days: int = 90
    user_surfaces: frozenset[str] = frozenset({"web", "app", "cli", "console"})
    # Whether anybody who reaches the web site may make their own user there (off: administrators make them)
    web_signup: bool = False
    # Surfaces where a client signs its people in (POST /v1/accounts/login or register): there, an account that
    # is not signed in as a user cannot talk to Clara
    login_surfaces: frozenset[str] = frozenset({"discord"})
    # The Discord bot built into the server (discord_bot/service.py): its token, whether it starts with the server,
    # and the invite link shown on the web site (empty: built from the token)
    discord_token: str | None = field(default=None, repr=False)
    discord_auto_start: bool = False
    discord_invite_url: str = ""
    # Gemini, DeepSeek and Mistral (providers.py), by id; and whether DeepSeek thinks before answering
    api_providers: dict[str, ApiProvider] = field(default_factory=dict)
    deepseek_thinking: bool = True
    # Projects (projects.py): what one may hold, the share of the context window under which its files are all
    # put in the prompt (beyond, Clara reads them with tools), and the token that reaches private GitHub repos
    project_max_bytes: int = 20_000_000
    project_max_files: int = 5_000
    project_inline_percent: int = 40
    github_token: str | None = field(default=None, repr=False)
    # Tokens a day a user may use when an administrator set no limit for them (limits.py); 0: no limit.
    # Administrators have none. `/limit default` changes it while the server runs.
    default_daily_tokens: int = 0
    # Billions of parameters a model of weight 1 has: a bigger one costs proportionally more credits a token
    # (models.py), a smaller one less
    weight_reference_b: float = 8.0
    # Reminders sent for one task before it is left alone (tasks.py): after that the person decides
    task_max_reminders: int = 10
    # Integrations (integrations/): the key that encrypts the accounts people connect (empty: one is made in the
    # data directory), the Google OAuth client (Drive), the address the server is reached at (Google redirects
    # there), and when a request for permission that nobody answered is pushed to other surfaces, and dropped
    secret_key: str = field(default="", repr=False)
    google_client_id: str = ""
    google_client_secret: str = field(default="", repr=False)
    public_url: str = ""
    approval_notify_after: int = 60
    approval_expire_after: int = 86_400

    @property
    def secret_key_file(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def unrestricted_clients(self) -> list[str]:
        """Clients allowed to speak for any surface (no CLARA_CLIENT_SURFACES entry)."""
        return sorted(set(self.tokens.values()) - set(self.client_surfaces))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clara.sqlite"

    @property
    def runtime_state_file(self) -> Path:
        return self.data_dir / "runtime.json"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        if env is None:
            load_dotenv()
            env = os.environ

        def text(key: str, default: str = "") -> str:
            return env.get(key, "").strip() or default

        tokens = parse_tokens(env.get("CLARA_TOKENS", ""))
        if not tokens:
            raise SettingsError(
                "CLARA_TOKENS is empty: the API refuses to start without credentials.\n"
                "Generate a token with:\n"
                '  python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
                "and put it in .env as CLARA_TOKENS=terminal:<token>"
            )
        admin_tokens = parse_tokens(env.get("CLARA_ADMIN_TOKENS", ""))
        for token in (*tokens, *admin_tokens):
            if token.lower().startswith(PLACEHOLDER_TOKEN):
                raise SettingsError(
                    "A token still has the placeholder value of .env.example. Generate a real one with:\n"
                    '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
                )
        if set(tokens) & set(admin_tokens):
            raise SettingsError("A token cannot be both a chat token and an admin token.")

        tailscale = text("CLARA_TAILSCALE", "off").lower()
        if tailscale not in TAILSCALE_MODES:
            raise SettingsError(f"CLARA_TAILSCALE must be one of {', '.join(TAILSCALE_MODES)}")
        tailscale_port = _positive_int(env, "CLARA_TAILSCALE_PORT", 443)
        if tailscale_port not in HTTPS_PORTS:
            raise SettingsError(
                f"CLARA_TAILSCALE_PORT must be one of {', '.join(map(str, HTTPS_PORTS))} (Tailscale's HTTPS ports)"
            )
        if tailscale == "funnel":
            weak = sorted(
                {name for token, name in (*tokens.items(), *admin_tokens.items()) if len(token) < MIN_PUBLIC_TOKEN_LENGTH}
            )
            if weak:
                raise SettingsError(
                    f"CLARA_TAILSCALE=funnel puts the server on the public internet: the token(s) of {', '.join(weak)} "
                    f"have fewer than {MIN_PUBLIC_TOKEN_LENGTH} characters. Generate real ones with:\n"
                    '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
                )

        client_surfaces = parse_client_surfaces(
            env.get("CLARA_CLIENT_SURFACES", ""), set(tokens.values())
        )

        api_key = text("OLLAMA_API_KEY") or None
        default_provider = text("CLARA_PROVIDER", "local").lower()
        if default_provider not in PROVIDER_IDS:
            raise SettingsError(f"CLARA_PROVIDER must be one of {', '.join(PROVIDER_IDS)}")
        if default_provider == "cloud" and not api_key:
            raise SettingsError("CLARA_PROVIDER=cloud needs OLLAMA_API_KEY.")
        api_providers = {
            name: ApiProvider(
                key_name,
                text(f"CLARA_{name.upper()}_HOST", host).rstrip("/"),
                text(f"CLARA_{name.upper()}_MODEL", model),
                _positive_int(env, f"CLARA_{name.upper()}_CONTEXT_WINDOW", window),
                text(key_name) or None,
            )
            for name, (key_name, host, model, window) in API_PROVIDERS.items()
        }
        if default_provider in api_providers and not api_providers[default_provider].api_key:
            raise SettingsError(
                f"CLARA_PROVIDER={default_provider} needs {api_providers[default_provider].key_name}."
            )
        inline_percent = _non_negative_int(env, "CLARA_PROJECT_INLINE_PERCENT", 40)
        if inline_percent > 90:
            raise SettingsError("CLARA_PROJECT_INLINE_PERCENT is a share of the context window: at most 90")

        return cls(
            host=text("CLARA_HOST", "127.0.0.1"),
            port=_positive_int(env, "CLARA_PORT", 8765),
            data_dir=Path(text("CLARA_DATA_DIR", "data")),
            tokens=tokens,
            admin_tokens=admin_tokens,
            client_surfaces=client_surfaces,
            history_turns=_positive_int(env, "CLARA_HISTORY_TURNS", 20),
            max_concurrent_llm=_positive_int(env, "CLARA_MAX_CONCURRENT_LLM", 2),
            max_tool_rounds=_positive_int(env, "CLARA_MAX_TOOL_ROUNDS", 40),
            tool_timeout=_positive_int(env, "CLARA_TOOL_TIMEOUT", 900),
            llm_first_token_timeout=_positive_int(env, "CLARA_LLM_FIRST_TOKEN_TIMEOUT", 300),
            llm_idle_timeout=_positive_int(env, "CLARA_LLM_IDLE_TIMEOUT", 120),
            llm_retries=_non_negative_int(env, "CLARA_LLM_RETRIES", 3),
            llm_retry_delay=_positive_int(env, "CLARA_LLM_RETRY_DELAY", 2),
            compact_percent=_non_negative_int(env, "CLARA_COMPACT_PERCENT", 80),
            keep_recent_turns=_non_negative_int(env, "CLARA_KEEP_RECENT_TURNS", 2),
            facts_token_budget=_positive_int(env, "CLARA_FACTS_TOKEN_BUDGET", 2000),
            purge_summarised=_flag(env, "CLARA_PURGE_SUMMARISED"),
            reminder_ai_timeout=_non_negative_int(env, "CLARA_REMINDER_AI_TIMEOUT", 60),
            system_prompt_file=Path(text("CLARA_SYSTEM_PROMPT_FILE", "config/system_prompt.md")),
            default_provider=default_provider,
            local_host=text("OLLAMA_HOST", DEFAULT_LOCAL_HOST),
            local_model=text("CLARA_LOCAL_MODEL", "llama3.2"),
            local_context_window=_positive_int(env, "CLARA_LOCAL_CONTEXT_WINDOW", 32_768),
            cloud_host=text("CLARA_CLOUD_HOST", DEFAULT_CLOUD_HOST),
            cloud_model=text("CLARA_CLOUD_MODEL", "gpt-oss:120b"),
            cloud_context_window=_positive_int(env, "CLARA_CLOUD_CONTEXT_WINDOW", 131_072),
            ollama_api_key=api_key,
            web_tools=_flag(env, "CLARA_WEB_TOOLS", default=True),
            notify_long_turn=_non_negative_int(env, "CLARA_NOTIFY_LONG_TURN", 120),
            traffic_log=_flag(env, "CLARA_TRAFFIC_LOG", default=True),
            traffic_log_days=_positive_int(env, "CLARA_TRAFFIC_LOG_DAYS", 30),
            traffic_log_max_body=_positive_int(env, "CLARA_TRAFFIC_LOG_MAX_BODY", 100_000),
            tailscale=tailscale,
            tailscale_port=tailscale_port,
            tailscale_bin=text("CLARA_TAILSCALE_BIN", "tailscale"),
            auth_max_failures=_non_negative_int(env, "CLARA_AUTH_MAX_FAILURES", 10),
            auth_block_seconds=_positive_int(env, "CLARA_AUTH_BLOCK_SECONDS", 300),
            session_days=_non_negative_int(env, "CLARA_SESSION_DAYS", 90),
            user_surfaces=parse_user_surfaces(text("CLARA_USER_SURFACES", "web,app,cli,console")),
            web_signup=_flag(env, "CLARA_WEB_SIGNUP"),
            login_surfaces=parse_login_surfaces(text("CLARA_LOGIN_SURFACES", "discord")),
            discord_token=text("DISCORD_BOT_TOKEN") or None,
            discord_auto_start=_flag(env, "AUTO_START_DISCORD_BOT"),
            discord_invite_url=text("DISCORD_BOT_INVIT_URL"),
            api_providers=api_providers,
            deepseek_thinking=_flag(env, "CLARA_DEEPSEEK_THINKING", default=True),
            project_max_bytes=_positive_int(env, "CLARA_PROJECT_MAX_MB", 20) * 1_000_000,
            project_max_files=_positive_int(env, "CLARA_PROJECT_MAX_FILES", 5_000),
            project_inline_percent=inline_percent,
            github_token=text("GITHUB_TOKEN") or None,
            default_daily_tokens=_token_limit(env, "CLARA_DEFAULT_DAILY_TOKENS"),
            weight_reference_b=_positive_number(env, "CLARA_WEIGHT_REFERENCE_B", 8.0),
            task_max_reminders=_positive_int(env, "CLARA_TASK_MAX_REMINDERS", 10),
            secret_key=text("CLARA_SECRET_KEY"),
            google_client_id=text("GOOGLE_CLIENT_ID"),
            google_client_secret=text("GOOGLE_CLIENT_SECRET"),
            public_url=text("CLARA_PUBLIC_URL").rstrip("/"),
            approval_notify_after=_non_negative_int(env, "CLARA_APPROVAL_NOTIFY_AFTER", 60),
            approval_expire_after=_positive_int(env, "CLARA_APPROVAL_EXPIRE_AFTER", 86_400),
        )
