"""Clara's second brain: an Obsidian vault shared with the person (see vault.py)."""

from .build import open_vault, owner_check
from .embeddings import EmbeddingError, EmbeddingIndex, OllamaEmbedder
from .errors import VaultError
from .git import GitError, GitSync
from .schema import Schema, load_schema
from .vault import Vault

__all__ = [
    "EmbeddingError", "EmbeddingIndex", "GitError", "GitSync", "OllamaEmbedder", "Schema", "Vault", "VaultError",
    "load_schema", "open_vault", "owner_check",
]
