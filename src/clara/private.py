"""Files only their owner may read: the database, the logs, the secret key, the backups of `.env`.

Everything the server keeps about people is in the data directory, so that directory is made private (`rwx------`) and
the files in it `rw-------`, each time the server starts, whatever the umask of the account that made them. Windows
has no such modes (the profile folder is private enough): nothing is done there.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def _supported() -> bool:
    return os.name == "posix"


def private_dir(path: Path) -> None:
    """Make the directory (and its parents, when new) and keep others out of it."""
    path.mkdir(parents=True, exist_ok=True)
    if _supported():
        with contextlib.suppress(OSError):
            os.chmod(path, DIR_MODE)


def private_file(path: Path) -> None:
    if _supported():
        with contextlib.suppress(OSError):
            os.chmod(path, FILE_MODE)


def harden_tree(root: Path) -> None:
    """The directory and everything in it, private. Links are left alone (their targets are not ours to change)."""
    if not _supported() or not root.is_dir():
        return
    private_dir(root)
    for folder, dirs, files in os.walk(root):
        for name in dirs:
            path = Path(folder, name)
            if not path.is_symlink():
                private_dir(path)
        for name in files:
            path = Path(folder, name)
            if not path.is_symlink():
                private_file(path)


def write_private_text(path: Path, text: str) -> None:
    """Write a file that is never readable by others, not even for a moment (it is created with the mode)."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE if _supported() else 0o666)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    private_file(path)  # a file that already existed keeps its old mode otherwise
