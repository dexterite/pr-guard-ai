"""Collect files for analysis based on check-specific and global configuration."""

import os
import re
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Binary / generated extensions to always skip
# ---------------------------------------------------------------------------

BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".zip", ".tar", ".gz", ".bz2", ".rar", ".7z",
    ".exe", ".dll", ".so", ".dylib", ".bin",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".pyc", ".pyo", ".class", ".o", ".obj",
    ".db", ".sqlite", ".sqlite3",
    ".min.js", ".min.css", ".map",
    ".wasm", ".parquet", ".avro",
})

DEFAULT_EXCLUDES = [
    "**/.git/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/venv/**",
    "**/.venv/**",
    "**/dist/**",
    "**/build/**",
    "**/.next/**",
    "**/coverage/**",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
    "**/*.lock",
    "**/package-lock.json",
    "**/yarn.lock",
    "**/poetry.lock",
    "**/Pipfile.lock",
    "**/go.sum",
    "**/composer.lock",
    "**/.terraform/**",
    "**/.terragrunt-cache/**",
]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_files(check_config, global_config):
    """Return a sorted list of file paths matching the check's criteria."""
    include_patterns = check_config.get("file_patterns", ["**/*"])
    exclude_patterns = (
        list(DEFAULT_EXCLUDES)
        + check_config.get("exclude_patterns", [])
        + global_config.get("exclude_patterns", [])
    )
    max_size_kb = global_config.get("max_file_size_kb", 100)
    diff_only = global_config.get("diff_only", True)

    # Get candidate files
    candidates = _get_changed_files() if diff_only else _get_all_tracked_files()
    if not candidates:
        return []

    matched = []
    for filepath in candidates:
        # Skip binary extensions
        ext = _ext(filepath)
        if ext in BINARY_EXTENSIONS:
            continue

        # Include patterns
        if not _matches_any(filepath, include_patterns):
            continue

        # Exclude patterns
        if _matches_any(filepath, exclude_patterns):
            continue

        # File must exist and be within size limit
        if not os.path.isfile(filepath):
            continue
        try:
            size_kb = os.path.getsize(filepath) / 1024
        except OSError:
            continue
        if size_kb > max_size_kb:
            continue

        # Heuristic binary check
        if _is_binary_file(filepath):
            continue

        matched.append(filepath)

    return sorted(set(matched))


def read_file_content(filepath, max_lines=2000):
    """Read file content safely. Returns ``(content, truncated)``."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()

        truncated = len(lines) > max_lines
        content = "".join(lines[:max_lines])
        if truncated:
            content += f"\n... (truncated — {len(lines) - max_lines} more lines)\n"
        return content, truncated
    except Exception as exc:
        return f"(error reading file: {exc})", False


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _get_changed_files():
    """Get the list of changed files in the current PR or push."""
    base_ref = os.environ.get("GITHUB_BASE_REF", "")

    if base_ref:
        # PR context
        _run_git(["fetch", "origin", base_ref, "--depth=1"])
        result = _run_git(
            ["diff", "--name-only", "--diff-filter=ACMRT", f"origin/{base_ref}...HEAD"]
        )
    else:
        event_before = os.environ.get("GITHUB_EVENT_BEFORE", "")
        if event_before and event_before != "0" * 40:
            result = _run_git(
                ["diff", "--name-only", "--diff-filter=ACMRT", f"{event_before}...HEAD"]
            )
        else:
            result = _run_git(["diff", "--name-only", "--diff-filter=ACMRT", "HEAD~1"])

    if result is None:
        print("::warning::git diff failed — falling back to all tracked files")
        return _get_all_tracked_files()

    return [f.strip() for f in result.strip().split("\n") if f.strip()]


def _get_all_tracked_files():
    """Return all git-tracked files."""
    result = _run_git(["ls-files"])
    if result is None:
        return []
    return [f.strip() for f in result.strip().split("\n") if f.strip()]


def _run_git(args):
    """Run a git command and return stdout, or None on failure."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.stdout if proc.returncode == 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Glob → regex matcher (supports **/)
# ---------------------------------------------------------------------------

_regex_cache: dict[str, re.Pattern] = {}


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Convert a glob pattern (with ``**/`` support) to a compiled regex."""
    if pattern in _regex_cache:
        return _regex_cache[pattern]

    pat = pattern.replace("\\", "/")
    parts: list[str] = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if i + 1 < len(pat) and pat[i + 1] == "*":
                # **
                if i + 2 < len(pat) and pat[i + 2] == "/":
                    parts.append("(?:.+/)?")
                    i += 3
                    continue
                else:
                    parts.append(".*")
                    i += 2
                    continue
            else:
                parts.append("[^/]*")
        elif c == "?":
            parts.append("[^/]")
        elif c in ".+^${}()|[]!\\":
            parts.append("\\" + c)
        else:
            parts.append(c)
        i += 1

    compiled = re.compile("^" + "".join(parts) + "$")
    _regex_cache[pattern] = compiled
    return compiled


def _matches_any(filepath: str, patterns: list[str]) -> bool:
    """Return True if *filepath* matches at least one glob pattern."""
    filepath = filepath.replace("\\", "/")
    for pattern in patterns:
        regex = _glob_to_regex(pattern)
        if regex.match(filepath):
            return True
    return False


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _ext(filepath: str) -> str:
    """Return the lowercased extension, including compound ones like .min.js."""
    name = os.path.basename(filepath).lower()
    # Handle compound extensions
    for compound in (".min.js", ".min.css"):
        if name.endswith(compound):
            return compound
    return os.path.splitext(name)[1]


def _is_binary_file(filepath: str, chunk_size: int = 8192) -> bool:
    """Heuristic: file is binary if its first chunk contains null bytes."""
    try:
        with open(filepath, "rb") as fh:
            chunk = fh.read(chunk_size)
        return b"\x00" in chunk
    except Exception:
        return True
