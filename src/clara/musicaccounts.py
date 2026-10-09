"""Each person's Music Assistant: the player of the PC they control, and the token of their own Music Assistant user.

Both are kept in the database, the token encrypted (integrations/vault.py) and never given back: the Music page and
the music_* tools only ever see the player. A person who saved neither has no music tools to use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .integrations.vault import Vault, VaultError
from .memory import Memory
from .music import MusicAssistant, MusicError

MAX_PLAYER = 200
MAX_TOKEN = 2000


@dataclass(frozen=True)
class SavedMusic:
    player: str  # the Music Assistant id of the player
    saved_at: str


class MusicAccounts:
    def __init__(self, memory: Memory, vault: Vault):
        self._memory = memory
        self._vault = vault

    def saved(self, person_id: int) -> SavedMusic | None:
        with self._memory.lock:
            row = self._memory.database.execute(
                "SELECT player, updated_at FROM user_music WHERE person_id = ?", (person_id,)
            ).fetchone()
        return SavedMusic(row["player"], row["updated_at"]) if row else None

    def save(self, person_id: int, player: str, token: str) -> SavedMusic:
        """Keep the person's player and token (replacing the earlier ones). Checking them is up to the caller."""
        created = datetime.now(UTC).isoformat(timespec="seconds")
        with self._memory.lock, self._memory.database as db:
            db.execute(
                "INSERT INTO user_music (person_id, player, token, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (person_id) DO UPDATE SET player = excluded.player, token = excluded.token,"
                " updated_at = excluded.updated_at",
                (person_id, player, self._vault.seal(token), created),
            )
        return SavedMusic(player, created)

    def choose(self, person_id: int, player: str) -> SavedMusic:
        """Another player for the person, with the token they saved (MusicError when they saved none)."""
        created = datetime.now(UTC).isoformat(timespec="seconds")
        with self._memory.lock, self._memory.database as db:
            changed = db.execute(
                "UPDATE user_music SET player = ?, updated_at = ? WHERE person_id = ?", (player, created, person_id)
            ).rowcount
        if not changed:
            raise MusicError("Music is not set up for you yet: choose your player and token on the Music page.")
        return SavedMusic(player, created)

    def remove(self, person_id: int) -> bool:
        with self._memory.lock, self._memory.database as db:
            removed = db.execute("DELETE FROM user_music WHERE person_id = ?", (person_id,)).rowcount
        return bool(removed)

    def client(self, music: MusicAssistant, person_id: int) -> MusicAssistant:
        """Music Assistant for one person: their player, with their token. MusicError when they saved none."""
        with self._memory.lock:
            row = self._memory.database.execute(
                "SELECT player, token FROM user_music WHERE person_id = ?", (person_id,)
            ).fetchone()
        if row is None:
            raise MusicError("Music is not set up for you yet: choose your player and token on the Music page.")
        try:
            token = self._vault.open(row["token"])
        except VaultError:
            raise MusicError("Your Music Assistant token can no longer be read: save it again on the Music page.") from None
        return music.for_player(row["player"], token or None)
