"""Load and merge configuration from environment variables, config files, and check definitions."""

import os
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULTS = {
    "api_base_url": "https://api.openai.com/v1",
    "model": "gpt-4o",
    "checks": "all",
    "full_scan": False,
    "diff_only": True,
    "severity_threshold": "low",
    "output_format": "markdown",
    "ship_to": "github-summary",
    "ship_webhook_url": "",
    "ship_file_path": "pr-guard-report",
    "max_file_size_kb": 100,
    "max_context_tokens": 100000,
    "exclude_patterns": [],
    "custom_checks_dir": "",
    "github_token": "",
    "config_file": "",
    "debug": False,
}

ALL_BUILTIN_CHECKS = [
    "code-quality",
    "sast",
    "secret-detection",
    "iac-security",
    "container-security",
]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config():
    """Build the full configuration dictionary.

    Merge order (last wins):
        built-in defaults → env vars → user config file → per-check configs
    """
    action_path = os.environ.get(
        "PRGUARD_ACTION_PATH", str(Path(__file__).resolve().parent.parent)
    )

    # Start with defaults
    config = dict(DEFAULTS)
    config["action_path"] = action_path

    # --- Required inputs -------------------------------------------------
    config["api_key"] = os.environ.get("PRGUARD_API_KEY", "")
    if not config["api_key"]:
        raise SystemExit("::error::Missing required input: api-key")

    # --- Env-var overrides -----------------------------------------------
    _ENV_MAP = {
        "PRGUARD_API_BASE_URL": ("api_base_url", "str"),
        "PRGUARD_MODEL": ("model", "str"),
        "PRGUARD_CHECKS": ("checks", "str"),
        "PRGUARD_FULL_SCAN": ("full_scan", "bool"),
        "PRGUARD_DIFF_ONLY": ("diff_only", "bool"),
        "PRGUARD_SEVERITY_THRESHOLD": ("severity_threshold", "str"),
        "PRGUARD_OUTPUT_FORMAT": ("output_format", "str"),
        "PRGUARD_SHIP_TO": ("ship_to", "str"),
        "PRGUARD_SHIP_WEBHOOK_URL": ("ship_webhook_url", "str"),
        "PRGUARD_SHIP_FILE_PATH": ("ship_file_path", "str"),
        "PRGUARD_MAX_FILE_SIZE_KB": ("max_file_size_kb", "int"),
        "PRGUARD_MAX_CONTEXT_TOKENS": ("max_context_tokens", "int"),
        "PRGUARD_EXCLUDE_PATTERNS": ("exclude_patterns", "csv"),
        "PRGUARD_CUSTOM_CHECKS_DIR": ("custom_checks_dir", "str"),
        "PRGUARD_GITHUB_TOKEN": ("github_token", "str"),
        "PRGUARD_CONFIG_FILE": ("config_file", "str"),
        "PRGUARD_DEBUG": ("debug", "bool"),
    }

    for env_var, (key, kind) in _ENV_MAP.items():
        raw = os.environ.get(env_var, "")
        if not raw:
            continue
        if kind == "bool":
            config[key] = raw.lower() in ("true", "1", "yes")
        elif kind == "int":
            try:
                config[key] = int(raw)
            except ValueError:
                pass
        elif kind == "csv":
            config[key] = [p.strip() for p in raw.split(",") if p.strip()]
        else:
            config[key] = raw

    # --- full-scan overrides diff-only ------------------------------------
    if config["full_scan"]:
        config["diff_only"] = False

    # --- User config file ------------------------------------------------
    config["user_overrides"] = _load_user_config(config)

    # --- Resolve enabled checks ------------------------------------------
    config["enabled_checks"] = _resolve_checks(config)

    # --- Load check definitions (prompt + config) ------------------------
    config["check_definitions"] = _load_check_definitions(config)

    # Log check loading result
    loaded = list(config["check_definitions"].keys())
    if len(loaded) < len(config["enabled_checks"]):
        missing = set(config["enabled_checks"]) - set(loaded)
        print(f"::warning::Failed to load checks: {', '.join(missing)}")
    if config.get("debug"):
        print(f"  [debug] Loaded check definitions: {', '.join(loaded)}")

    return config


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_user_config(config):
    """Locate and parse the user's pr-guard config file."""
    explicit = config.get("config_file", "")
    if explicit and os.path.isfile(explicit):
        return _read_yaml(explicit)

    for candidate in ("pr-guard.config.yml", "pr-guard.config.yaml", ".pr-guard.yml"):
        if os.path.isfile(candidate):
            return _read_yaml(candidate)

    return {}


def _read_yaml(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _resolve_checks(config):
    """Determine which checks are enabled."""
    checks_input = config.get("checks", "all")

    if checks_input == "all":
        enabled = list(ALL_BUILTIN_CHECKS)
    else:
        enabled = [c.strip() for c in checks_input.split(",") if c.strip()]

    # If user has a custom checks dir, discover additional checks there
    custom_dir = config.get("custom_checks_dir", "")
    if custom_dir and os.path.isdir(custom_dir):
        for entry in sorted(os.listdir(custom_dir)):
            check_path = os.path.join(custom_dir, entry)
            if os.path.isdir(check_path) and entry not in enabled:
                # Only add if it has a prompt
                if os.path.isfile(os.path.join(check_path, "prompt.md")):
                    enabled.append(entry)

    # Apply user overrides (enable/disable)
    user_checks = config.get("user_overrides", {}).get("checks", {})
    if isinstance(user_checks, dict):
        for check_name, check_conf in user_checks.items():
            if not isinstance(check_conf, dict):
                continue
            if check_conf.get("enabled") is False:
                enabled = [c for c in enabled if c != check_name]
            elif check_name not in enabled and check_conf.get("enabled", True):
                enabled.append(check_name)

    return enabled


def _load_check_definitions(config):
    """Load prompt.md and config.yml for every enabled check."""
    definitions = {}
    action_path = Path(config["action_path"])

    for check_name in config["enabled_checks"]:
        builtin_dir = action_path / "checks" / check_name
        custom_dir = (
            Path(config["custom_checks_dir"]) / check_name
            if config.get("custom_checks_dir")
            else None
        )

        # --- Prompt -------------------------------------------------------
        prompt_path = None
        if custom_dir and (custom_dir / "prompt.md").is_file():
            prompt_path = custom_dir / "prompt.md"
        elif (builtin_dir / "prompt.md").is_file():
            prompt_path = builtin_dir / "prompt.md"

        if not prompt_path:
            print(f"::warning::No prompt.md for check '{check_name}' — skipping")
            continue

        prompt_text = prompt_path.read_text(encoding="utf-8")

        # --- Check-level config -------------------------------------------
        check_config = {}
        if (builtin_dir / "config.yml").is_file():
            check_config = _read_yaml(str(builtin_dir / "config.yml"))

        if custom_dir and (custom_dir / "config.yml").is_file():
            check_config = _deep_merge(
                check_config, _read_yaml(str(custom_dir / "config.yml"))
            )

        # User overrides for this specific check
        user_check = (
            config.get("user_overrides", {}).get("checks", {}).get(check_name, {})
        )
        if isinstance(user_check, dict):
            check_config = _deep_merge(check_config, user_check)

            # Append extra prompt instructions
            extra = user_check.get("extra_instructions", "")
            if extra:
                prompt_text += f"\n\n## Additional Instructions\n\n{extra}"

        definitions[check_name] = {
            "name": check_name,
            "prompt": prompt_text,
            "config": check_config,
        }

    return definitions


def _deep_merge(base, override):
    """Recursively merge *override* into *base*. Override values win."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        elif key in result and isinstance(result[key], list) and isinstance(val, list):
            result[key] = list(result[key]) + list(val)
        else:
            result[key] = val
    return result
