"""The key that keeps the accounts people connect (a GitHub token, a Google refresh token) unreadable in the database.

The key is `CLARA_SECRET_KEY` (a Fernet key, or any long text: it is hashed into one), else one made on first use
and kept in `data/secret.key`. Whoever holds both the database and the key can read the accounts, so the key file
is made readable by its owner only (where the system lets it). The API never gives a secret back.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class VaultError(ValueError):
    """A secret could not be opened (the key changed, or the stored text is damaged)."""


def _fernet_key(text: str) -> bytes:
    text = text.strip()
    try:  # already a Fernet key: 32 bytes, url-safe base64
        if len(base64.urlsafe_b64decode(text.encode())) == 32:
            return text.encode()
    except (ValueError, base64.binascii.Error):
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest())


class Vault:
    def __init__(self, key: str = "", key_file: Path | None = None):
        if not key:
            if key_file is None:
                raise VaultError("The vault needs a key or a key file.")
            key = self._load_or_make(key_file)
        self._fernet = Fernet(_fernet_key(key))

    @staticmethod
    def _load_or_make(path: Path) -> str:
        try:
            return path.read_text(encoding="ascii").strip() or Vault._make(path)
        except FileNotFoundError:
            return Vault._make(path)

    @staticmethod
    def _make(path: Path) -> str:
        key = Fernet.generate_key().decode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key, encoding="ascii")
        with contextlib.suppress(OSError):  # Windows has no such modes: the profile folder is private enough
            os.chmod(path, 0o600)
        return key

    def seal(self, text: str) -> str:
        """The text, encrypted."""
        return self._fernet.encrypt(text.encode("utf-8")).decode("ascii") if text else ""

    def open(self, sealed: str) -> str:
        if not sealed:
            return ""
        try:
            return self._fernet.decrypt(sealed.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            raise VaultError(
                "A saved account cannot be read: the secret key changed since it was connected. Connect it again."
            ) from None
