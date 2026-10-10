"""The error of the vault: a request that cannot be done, whose message says what to do instead."""


class VaultError(ValueError):
    """A request that cannot be done; the message says why, and what to do instead."""
