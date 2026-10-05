"""Builds the integrations of a server: the store, the vault, the connectors, the broker the tools use and the service
that follows the requests for permission."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..memory import Memory
from ..notifications import Notifier
from ..settings import Settings
from .approvals import Approvals
from .broker import Broker
from .connectors.base import Connector
from .connectors.computer import ComputerFolders
from .connectors.gdrive import GoogleDrive
from .connectors.github_live import GitHubLive
from .connectors.serverfs import ServerFolders
from .permissions import COMPUTER, GDRIVE, GITHUB, SERVER
from .store import IntegrationStore
from .vault import Vault


@dataclass
class Integrations:
    store: IntegrationStore
    vault: Vault
    broker: Broker
    approvals: Approvals
    connectors: dict[str, Connector]
    settings: Settings
    google_states: dict[str, float] = field(default_factory=dict)  # sign-in links already used (until they expire)


def build(memory: Memory, settings: Settings, notifier: Notifier, extra: dict[str, Connector] | None = None) -> Integrations:
    store = IntegrationStore(memory)
    vault = Vault(settings.secret_key, settings.secret_key_file)
    connectors: dict[str, Connector] = {
        SERVER: ServerFolders(lambda: store.policy()["roots"]), GITHUB: GitHubLive(), COMPUTER: ComputerFolders(store, notifier),
    }
    if settings.google_client_id and settings.google_client_secret:
        connectors[GDRIVE] = GoogleDrive(settings.google_client_id, settings.google_client_secret)
    connectors.update(extra or {})
    broker = Broker(memory, store, vault, connectors)
    approvals = Approvals(
        broker, store, memory, notifier, settings.approval_notify_after, settings.approval_expire_after
    )
    return Integrations(store, vault, broker, approvals, connectors, settings)
