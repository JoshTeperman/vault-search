"""Environment and path settings. No hostnames or vault content."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from a .env file if present.

    Existing environment variables win. Values must never be committed;
    this only reads a local, gitignored file.
    """
    path = path or Path.cwd() / ".env"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in _TRUE


def _path_from_env(*names: str) -> Path | None:
    for name in names:
        raw = os.environ.get(name)
        if raw:
            return Path(raw).expanduser()
    return None


@dataclass(frozen=True)
class Settings:
    vault_root: Path
    index_path: Path
    include_raw: bool = False
    include_examples: bool = False

    @classmethod
    def from_env(
        cls,
        *,
        vault_root: Path | None = None,
        index_path: Path | None = None,
        include_raw: bool | None = None,
        include_examples: bool | None = None,
    ) -> Settings:
        load_dotenv()
        root = vault_root or _path_from_env("VAULT_ROOT", "VAULT_PATH")
        db = index_path or _path_from_env("INDEX_PATH")
        if root is None:
            raise ConfigError("set VAULT_ROOT or VAULT_PATH to the vault checkout")
        if db is None:
            db = Path.cwd() / "index" / "vault.db"
        return cls(
            vault_root=root.resolve(),
            index_path=db.expanduser().resolve(),
            include_raw=cls._flag(include_raw, "INDEX_INCLUDE_RAW", False),
            include_examples=cls._flag(include_examples, "INDEX_INCLUDE_EXAMPLES", False),
        )

    @staticmethod
    def _flag(explicit: bool | None, env_name: str, default: bool) -> bool:
        if explicit is not None:
            return explicit
        return _truthy(env_name, default)


class ConfigError(ValueError):
    """Missing or invalid configuration."""


