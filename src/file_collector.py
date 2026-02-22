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
# Module-level cache for git diff (avoid running git N times for N checks)
# ---------------------------------------------------------------------------

_changed_files_cache: list[str] | None = None
_all_files_cache: list[str] | None = None

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_files(check_config, global_config):
    """Return a sorted list of file paths matching the check's criteria.

    Logs skip-reason summary and detailed paths when ``debug`` is True.
    """
    debug = global_config.get("debug", False)

    include_patterns = check_config.get("file_patterns", ["**/*"])
    exclude_patterns = (
        list(DEFAULT_EXCLUDES)
        + check_config.get("exclude_patterns", [])
        + global_config.get("exclude_patterns", [])
    )
    max_size_kb = global_config.get("max_file_size_kb", 100)
    diff_only = global_config.get("diff_only", True)

    # Get candidate files — cached after first call
    candidates = _get_candidates(diff_only, debug)
    if not candidates:
        if debug:
            print("    [debug] No candidate files returned from git")
        return []

    if debug:
        print(f"    [debug] Candidates from git: {len(candidates)}")

    matched = []
    skip_reasons: dict[str, int] = {}

    for filepath in candidates:
        # Skip binary extensions
        ext = _ext(filepath)
        if ext in BINARY_EXTENSIONS:
            skip_reasons["binary_ext"] = skip_reasons.get("binary_ext", 0) + 1
            continue

        # Include patterns
        if not _matches_any(filepath, include_patterns):
            skip_reasons["no_pattern_match"] = skip_reasons.get("no_pattern_match", 0) + 1
            continue

        # Exclude patterns
        if _matches_any(filepath, exclude_patterns):
            skip_reasons["excluded"] = skip_reasons.get("excluded", 0) + 1
            continue

        # File must exist and be within size limit
        if not os.path.isfile(filepath):
            skip_reasons["not_found"] = skip_reasons.get("not_found", 0) + 1
            continue
        try:
            size_kb = os.path.getsize(filepath) / 1024
        except OSError:
            skip_reasons["os_error"] = skip_reasons.get("os_error", 0) + 1
            continue
        if size_kb > max_size_kb:
            skip_reasons["too_large"] = skip_reasons.get("too_large", 0) + 1
            if debug:
                print(f"    [debug] Skipped (>{max_size_kb}KB): {filepath} ({size_kb:.1f}KB)")
            continue

        # Heuristic binary check
        if _is_binary_file(filepath):
            skip_reasons["binary_content"] = skip_reasons.get("binary_content", 0) + 1
            continue

        matched.append(filepath)

    # Always log a compact summary of why files were filtered
    if skip_reasons:
        parts = [f"{v} {k}" for k, v in sorted(skip_reasons.items(), key=lambda x: -x[1])]
        print(f"    Filtered out : {', '.join(parts)}")

    if debug and matched:
        for f in matched[:20]:
            print(f"    [debug]  + {f}")
        if len(matched) > 20:
            print(f"    [debug]  ... and {len(matched) - 20} more")

    return sorted(set(matched))


def reset_cache():
    """Clear the file-list caches (useful for testing)."""
    global _changed_files_cache, _all_files_cache
    _changed_files_cache = None
    _all_files_cache = None


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


def _get_candidates(diff_only: bool, debug: bool = False) -> list[str]:
    """Return candidate file list, using a module-level cache."""
    global _changed_files_cache, _all_files_cache

    if diff_only:
        if _changed_files_cache is None:
            _changed_files_cache = _get_changed_files(debug)
        return _changed_files_cache
    else:
        if _all_files_cache is None:
            _all_files_cache = _get_all_tracked_files(debug)
        return _all_files_cache


def _get_changed_files(debug: bool = False) -> list[str]:
    """Get the list of changed files in the current PR or push.

    Tries multiple strategies with graceful fallback:
      1. PR diff (GITHUB_BASE_REF)
      2. Push diff (GITHUB_EVENT_BEFORE)
      3. HEAD~1 diff
      4. All tracked files
    """
    base_ref = os.environ.get("GITHUB_BASE_REF", "")

    # --- Strategy 1: PR context ------------------------------------------
    if base_ref:
        print(f"  Git context   : PR (base={base_ref})")
        _run_git(["fetch", "origin", base_ref, "--depth=1"], debug=debug)
        result = _run_git(
            ["diff", "--name-only", "--diff-filter=ACMRT", f"origin/{base_ref}...HEAD"],
            debug=debug,
        )
        if result is not None:
            files = _split_lines(result)
            print(f"  Changed files : {len(files)} (PR diff vs origin/{base_ref})")
            return files

    # --- Strategy 2: Push context ----------------------------------------
    event_before = os.environ.get("GITHUB_EVENT_BEFORE", "")
    if event_before and event_before != "0" * 40:
        print(f"  Git context   : push (before={event_before[:12]}…)")
        result = _run_git(
            ["diff", "--name-only", "--diff-filter=ACMRT", f"{event_before}...HEAD"],
            debug=debug,
        )
        if result is not None:
            files = _split_lines(result)
            print(f"  Changed files : {len(files)} (push diff)")
            return files

        # Shallow clone may not have the before commit — try fetching it
        print("    Push diff failed (shallow clone?). Fetching before-SHA…")
        _run_git(["fetch", "origin", event_before, "--depth=1"], debug=debug)
        result = _run_git(
            ["diff", "--name-only", "--diff-filter=ACMRT", f"{event_before}...HEAD"],
            debug=debug,
        )
        if result is not None:
            files = _split_lines(result)
            print(f"  Changed files : {len(files)} (push diff after fetch)")
            return files

    # --- Strategy 3: HEAD~1 fallback -------------------------------------
    print("  Git context   : fallback (HEAD~1)")
    result = _run_git(
        ["diff", "--name-only", "--diff-filter=ACMRT", "HEAD~1"], debug=debug
    )
    if result is not None:
        files = _split_lines(result)
        print(f"  Changed files : {len(files)} (HEAD~1)")
        return files

    # --- Strategy 4: all tracked files -----------------------------------
    print("::warning::All git diff strategies failed — scanning ALL tracked files")
    return _get_all_tracked_files(debug)


def _get_all_tracked_files(debug: bool = False) -> list[str]:
    """Return all git-tracked files."""
    result = _run_git(["ls-files"], debug=debug)
    if result is None:
        print("::warning::git ls-files failed — no files to analyze")
        return []
    files = _split_lines(result)
    print(f"  Tracked files : {len(files)} (full repo)")
    return files


def _run_git(args, debug: bool = False):
    """Run a git command and return stdout, or None on failure."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if debug:
            cmd = "git " + " ".join(args[:3])
            if len(args) > 3:
                cmd += " …"
            print(f"    [debug] {cmd} → exit={proc.returncode}")
            if proc.returncode != 0 and proc.stderr:
                err_line = proc.stderr.strip().split("\n")[0][:200]
                print(f"    [debug]   stderr: {err_line}")
        return proc.stdout if proc.returncode == 0 else None
    except Exception as exc:
        if debug:
            print(f"    [debug] git command exception: {exc}")
        return None


def _split_lines(text: str) -> list[str]:
    """Split git output into non-empty trimmed lines."""
    return [f.strip() for f in text.strip().split("\n") if f.strip()]


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
