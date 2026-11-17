"""Pull-only vault update: `git pull --ff-only` then reindex."""

from __future__ import annotations

import subprocess
from pathlib import Path

from vault_search.config import ConfigError
from vault_search.index import git_head


def pull_ff_only(root: Path) -> str:
    """Fast-forward the vault checkout. Raises if the root is not a git repo."""
    if git_head(root) is None:
        raise ConfigError(f"vault root is not a git checkout: {root}")
    result = subprocess.run(
        ["git", "-C", str(root), "pull", "--ff-only"],
        check=False,
        capture_output=True,
        text=True,
    )
    output = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        detail = err or output or f"exit {result.returncode}"
        raise ConfigError(f"git pull --ff-only failed: {detail}")
    return output or err or "already up to date"


def sync(client) -> tuple[str, object]:
    """Pull then reindex. Returns (pull message, ReindexStats)."""
    message = pull_ff_only(client.settings.vault_root)
    stats = client.reindex()
    return message, stats
