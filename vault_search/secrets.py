"""Shared secret detector: regex plus entropy rules, with scan and mask APIs.

Stdlib only, so vault ingest lint can reuse it without extra dependencies.

CLI::

    python -m vault_search.secrets scan <paths>

Exits 1 if any finding is reported. Findings print path, line, column and
rule name; the matched secret is never printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# Stable rule names. Vault lint and the service masker should depend on these.
RULE_GITHUB = "github_token"
RULE_OPENAI = "openai_api_key"
RULE_ANTHROPIC = "anthropic_api_key"
RULE_AWS_ACCESS_KEY = "aws_access_key_id"
RULE_SLACK = "slack_token"
RULE_GOOGLE = "google_api_key"
RULE_JWT = "jwt"
RULE_PRIVATE_KEY = "private_key"
RULE_GENERIC = "generic_secret"

DEFAULT_PLACEHOLDER_TEMPLATE = "[REDACTED:{rule}]"
GENERIC_ENTROPY_THRESHOLD = 4.0
GENERIC_MIN_DISTINCT = 8
GENERIC_MIN_LENGTH = 20
MAX_SCAN_FILE_BYTES = 2 * 1024 * 1024

_SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".cache",
    "node_modules",
    "dist",
    "build",
    ".eggs",
    "models",
}

_SKIP_FILE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".bin",
    ".gguf",
    ".onnx",
    ".safetensors",
    ".pt",
    ".pth",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tgz",
    ".whl",
    ".egg",
}

# Placeholders and template values that should not be treated as secrets.
_PLACEHOLDER_VALUE = re.compile(
    r"""(?ix)
    ^(
        \$\{[^}]+\}$
        | \$[A-Za-z_][A-Za-z0-9_]*$
        | <[^>]+>$
        | \[[^\]]+\]$
        | (your|my|the)[-_]?.*
        | (changeme|change_me|placeholder|example|sample|dummy|todo|fixme|redacted|secret|password|token|insert|replace|xxx+|yyy+|zzz+)
        | (none|null|true|false|n/?a)$
    )
    """
)

# Specific token patterns. More-specific prefixes are listed first.
# Capturing group 1 is the secret span (full match, including headers for PEMs).
_PATTERN_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        RULE_PRIVATE_KEY,
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"
            r".{20,}?"
            r"-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    (
        RULE_GITHUB,
        re.compile(
            r"(?:gh[pours]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9]{11,255})"
        ),
    ),
    (
        RULE_ANTHROPIC,
        re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),
    ),
    (
        RULE_OPENAI,
        re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9\-_]{32,}"),
    ),
    (
        RULE_AWS_ACCESS_KEY,
        re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    ),
    (
        RULE_SLACK,
        re.compile(r"(?:xox[baprs]|xapp)-[A-Za-z0-9-]{10,}"),
    ),
    (
        RULE_GOOGLE,
        re.compile(
            r"(?:AIza[0-9A-Za-z\-_]{35}|GOCSPX-[A-Za-z0-9\-_]{20,}|ya29\.[A-Za-z0-9\-_]{20,})"
        ),
    ),
    (
        RULE_JWT,
        re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
)

_GENERIC_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?:^|[\s,;{"'])
    (?:
        (?:api|access|auth|client|private|secret)[-_]?
        (?:key|token|secret|password|passwd)?
        | (?:api_key|access_key|secret_key|private_key)
        | (?:key|token|secret|password|passwd|authorization|bearer)
    )
    \s*[:=]\s*
    (?:bearer\s+)?
    [='" ]*
    (?P<value>[^\s'"]{20,})
    """
)


@dataclass(frozen=True)
class Finding:
    """A secret-like span. The matched text is intentionally omitted."""

    rule: str
    start: int
    end: int
    line: int
    column: int
    path: str | None = None


def shannon_entropy(text: str) -> float:
    """Shannon entropy in bits per character."""
    if not text:
        return 0.0
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in Counter(text).values())


def _line_column(text: str, index: int) -> tuple[int, int]:
    line = text.count("\n", 0, index) + 1
    last_nl = text.rfind("\n", 0, index)
    column = index + 1 if last_nl < 0 else index - last_nl
    return line, column


def _is_jwt(token: str) -> bool:
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        return False
    try:
        padded = parts[0] + ("=" * (-len(parts[0]) % 4))
        header = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return False
    return isinstance(header, dict) and "alg" in header


def _looks_placeholder(value: str) -> bool:
    stripped = value.strip().strip("'\"")
    if not stripped or _PLACEHOLDER_VALUE.match(stripped):
        return True
    if stripped.startswith("${") or stripped.startswith("$"):
        return True
    # Masker output, including when a later record is concatenated in SQLite pages.
    if stripped.startswith("[REDACTED:") or stripped.startswith("REDACTED:"):
        return True
    return False


def _generic_value_is_secret(value: str) -> bool:
    # Strip a trailing delimiter that often follows assignments.
    value = value.rstrip(",;)]}")
    if len(value) < GENERIC_MIN_LENGTH:
        return False
    if _looks_placeholder(value):
        return False
    if len(set(value)) < GENERIC_MIN_DISTINCT:
        return False
    return shannon_entropy(value) >= GENERIC_ENTROPY_THRESHOLD


def scan(text: str, *, path: str | None = None) -> list[Finding]:
    """Return secret-like spans in *text*, without including the secret values."""
    findings: list[Finding] = []
    seen: set[tuple[str, int, int]] = set()

    def add(rule: str, start: int, end: int) -> None:
        if start < 0 or end <= start or end > len(text):
            return
        key = (rule, start, end)
        if key in seen:
            return
        seen.add(key)
        line, column = _line_column(text, start)
        findings.append(
            Finding(
                rule=rule,
                start=start,
                end=end,
                line=line,
                column=column,
                path=path,
            )
        )

    for rule, pattern in _PATTERN_RULES:
        for match in pattern.finditer(text):
            if rule == RULE_JWT and not _is_jwt(match.group(0)):
                continue
            if rule == RULE_OPENAI and match.group(0).startswith("sk-ant-"):
                # Anthropic keys also match the looser OpenAI prefix; skip the duplicate.
                continue
            add(rule, match.start(), match.end())

    for match in _GENERIC_ASSIGNMENT.finditer(text):
        value = match.group("value")
        if not _generic_value_is_secret(value):
            continue
        # Point at the value, not the key name.
        add(RULE_GENERIC, match.start("value"), match.start("value") + len(value.rstrip(",;)]}")))

    findings.sort(key=lambda f: (f.start, f.end, f.rule))
    return findings


def _merged_spans(findings: list[Finding]) -> list[tuple[int, int, str]]:
    if not findings:
        return []
    ordered = sorted(findings, key=lambda f: (f.start, f.end))
    merged: list[tuple[int, int, str]] = []
    start, end, rule = ordered[0].start, ordered[0].end, ordered[0].rule
    for finding in ordered[1:]:
        if finding.start <= end:
            end = max(end, finding.end)
            if finding.rule != rule:
                rule = "secret"
        else:
            merged.append((start, end, rule))
            start, end, rule = finding.start, finding.end, finding.rule
    merged.append((start, end, rule))
    return merged


def mask(text: str, *, placeholder: str | None = None) -> str:
    """Replace every secret-like span with a placeholder.

    If *placeholder* is omitted, each span becomes ``[REDACTED:<rule>]``.
    The raw secret is never included in the result.
    """
    findings = scan(text)
    if not findings:
        return text
    out = text
    for start, end, rule in reversed(_merged_spans(findings)):
        token = placeholder if placeholder is not None else DEFAULT_PLACEHOLDER_TEMPLATE.format(rule=rule)
        out = out[:start] + token + out[end:]
    return out


def scan_path(path: Path) -> list[Finding]:
    """Scan a single file. Skips binaries, oversized files, and missing paths."""
    if not path.is_file() or path.is_symlink():
        return []
    if path.suffix.lower() in _SKIP_FILE_SUFFIXES:
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0 or size > MAX_SCAN_FILE_BYTES:
        return []
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in data:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return []
    display = path.as_posix()
    return scan(text, path=display)


def iter_scan_targets(paths: list[Path]) -> list[Path]:
    """Expand files and directories into a list of regular files to scan."""
    targets: list[Path] = []
    for raw in paths:
        path = raw
        if path.is_file():
            targets.append(path)
            continue
        if not path.is_dir():
            continue
        for child in path.rglob("*"):
            if not child.is_file() or child.is_symlink():
                continue
            if any(part in _SKIP_DIR_NAMES for part in child.parts):
                continue
            targets.append(child)
    return targets


def _format_finding(finding: Finding) -> str:
    location = finding.path or "<input>"
    return f"{location}:{finding.line}:{finding.column}: {finding.rule}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vault_search.secrets",
        description="Scan text or files for secret-like spans. Matched values are never printed.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan_p = sub.add_parser("scan", help="Scan files or directories; exit 1 if any finding is reported.")
    scan_p.add_argument("paths", nargs="+", type=Path, help="Files or directories to scan.")

    mask_p = sub.add_parser("mask", help="Read stdin and write masked text to stdout.")
    mask_p.add_argument("--placeholder", default=None, help="Fixed replacement string (default: [REDACTED:<rule>]).")

    args = parser.parse_args(argv)

    if args.command == "mask":
        text = sys.stdin.read()
        sys.stdout.write(mask(text, placeholder=args.placeholder))
        return 0

    findings: list[Finding] = []
    missing = []
    for path in args.paths:
        if not path.exists():
            missing.append(path)
            continue
        if path.is_file():
            findings.extend(scan_path(path))
        elif path.is_dir():
            for child in iter_scan_targets([path]):
                findings.extend(scan_path(child))
        else:
            missing.append(path)

    if missing:
        for path in missing:
            print(f"error: path not found: {path}", file=sys.stderr)
        return 2

    findings.sort(key=lambda f: (f.path or "", f.line, f.column, f.rule))
    for finding in findings:
        print(_format_finding(finding))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
