"""vault-search: agent-facing, read-only search over a private knowledge vault."""

from vault_search.client import Client
from vault_search.config import ConfigError, Settings
from vault_search.index import ReindexStats

__version__ = "0.1.0"
__all__ = [
    "Client",
    "ConfigError",
    "ReindexStats",
    "Settings",
]
