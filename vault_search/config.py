"""Environment and path settings. No hostnames or vault content."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from vault_search.embed import DEFAULT_MODEL

_TRUE = {"1", "true", "yes", "on"}

DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 8787
DEFAULT_MCP_PATH = "/vault/mcp"
DEFAULT_TAILSCALED_SOCKET = Path("/var/run/tailscale/tailscaled.sock")
NONLOCAL_BIND_HOSTS = frozenset({"0.0.0.0", "::", "[::]", "*"})


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


def _int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def load_denylist(path: Path | None, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    items: list[str] = list(extra)
    if path is not None and path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            entry = line.strip()
            if entry and not entry.startswith("#"):
                items.append(entry)
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return tuple(unique)


def assert_bind_host(host: str, allow_nonlocal: bool = False) -> str:
    """Refuse a wildcard bind unless LISTEN_ALLOW_NONLOCAL is set."""
    cleaned = (host or "").strip()
    if not cleaned:
        cleaned = DEFAULT_LISTEN_HOST
    key = cleaned.lower()
    if key in NONLOCAL_BIND_HOSTS and not allow_nonlocal:
        raise ConfigError(
            "refusing to bind to a non-loopback address "
            f"({cleaned}); set LISTEN_ALLOW_NONLOCAL=1 to override"
        )
    return cleaned


def normalize_mcp_path(path: str | None) -> str:
    value = (path or DEFAULT_MCP_PATH).strip() or DEFAULT_MCP_PATH
    if not value.startswith("/"):
        value = "/" + value
    return value.rstrip("/") or "/"


@dataclass(frozen=True)
class Settings:
    vault_root: Path
    index_path: Path
    include_raw: bool = False
    include_examples: bool = False
    embedding_model: str = DEFAULT_MODEL
    embedding_cache_dir: Path | None = None
    embedding_model_path: Path | None = None
    listen_host: str = DEFAULT_LISTEN_HOST
    listen_port: int = DEFAULT_LISTEN_PORT
    mcp_path: str = DEFAULT_MCP_PATH
    listen_allow_nonlocal: bool = False
    tailscaled_socket: Path = DEFAULT_TAILSCALED_SOCKET
    identity_source: str = "whois"
    audit_log_path: Path | None = None
    denylist_path: Path | None = None
    denylist: tuple[str, ...] = field(default_factory=tuple)

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
        denylist_path = _path_from_env("DENYLIST_PATH")
        cache = _path_from_env("EMBEDDING_CACHE_DIR")
        if cache is not None:
            cache = cache.expanduser()
        model_path = _path_from_env("EMBEDDING_MODEL_PATH")
        if model_path is not None:
            model_path = model_path.expanduser()
        audit = _path_from_env("AUDIT_LOG_PATH")
        if audit is not None:
            audit = audit.expanduser()
        socket = _path_from_env("TAILSCALED_SOCKET") or DEFAULT_TAILSCALED_SOCKET
        host = os.environ.get("LISTEN_HOST") or os.environ.get("MCP_HOST") or DEFAULT_LISTEN_HOST
        allow = _truthy("LISTEN_ALLOW_NONLOCAL", False)
        return cls(
            vault_root=root.resolve(),
            index_path=db.expanduser().resolve(),
            include_raw=cls._flag(include_raw, "INDEX_INCLUDE_RAW", False),
            include_examples=cls._flag(include_examples, "INDEX_INCLUDE_EXAMPLES", False),
            embedding_model=(os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            embedding_cache_dir=cache,
            embedding_model_path=model_path,
            listen_host=host.strip() or DEFAULT_LISTEN_HOST,
            listen_port=_int_from_env("LISTEN_PORT", _int_from_env("MCP_PORT", DEFAULT_LISTEN_PORT)),
            mcp_path=normalize_mcp_path(os.environ.get("MCP_PATH")),
            listen_allow_nonlocal=allow,
            tailscaled_socket=socket.expanduser(),
            identity_source=(os.environ.get("IDENTITY_SOURCE") or "whois").strip() or "whois",
            audit_log_path=audit,
            denylist_path=denylist_path,
            denylist=load_denylist(denylist_path, _csv(os.environ.get("DENYLIST"))),
        )

    @staticmethod
    def _flag(explicit: bool | None, env_name: str, default: bool) -> bool:
        if explicit is not None:
            return explicit
        return _truthy(env_name, default)


class ConfigError(ValueError):
    """Missing or invalid configuration."""
