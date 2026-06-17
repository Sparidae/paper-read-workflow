# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0", "python-dotenv>=1.0.0"]
# ///
"""Per-output-backend configuration loader with auto-provisioning.

When a backend config is missing or invalid, this module can either:

1. Prompt the user interactively via ``input()`` (when running in a terminal).
2. Raise ``BackendConfigError`` with structured ``missing`` fields so the caller
   can surface a prompt through the agent UI.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from _env_writer import write_env_values
from _lib import find_project_root, load_config


class BackendConfigError(RuntimeError):
    """Raised when a backend configuration is incomplete and cannot be resolved."""

    def __init__(
        self,
        backend: str,
        missing: list[dict[str, Any]],
        message: str = "",
    ):
        self.backend = backend
        self.missing = missing
        super().__init__(message or f"Backend {backend!r} needs configuration")


@dataclass(frozen=True)
class BackendField:
    """Description of one configurable backend field."""

    section: str
    key: str
    env_key: str
    description: str
    required: bool = True
    sensitive: bool = True


# Field definitions per backend / storage type.
_BACKEND_FIELDS: dict[str, list[BackendField]] = {
    "notion": [
        BackendField("auth", "token", "NOTION_TOKEN", "Notion Integration Token"),
        BackendField("auth", "database_id", "NOTION_DATABASE_ID", "Notion 数据库 ID"),
        BackendField(
            "auth",
            "parent_page_id",
            "NOTION_PARENT_PAGE_ID",
            "Notion 父页面 ID（自动建库时使用，可选）",
            required=False,
        ),
    ],
    "lark": [
        BackendField(
            "auth",
            "identity",
            "LARK_IDENTITY",
            "飞书身份类型（user 或 bot）",
            required=False,
            sensitive=False,
        ),
        BackendField(
            "storage",
            "parent_token",
            "LARK_PARENT_FOLDER_TOKEN",
            "飞书父文件夹 token（可选）",
            required=False,
        ),
    ],
}


def _backend_dir(name: str) -> Path:
    return find_project_root() / "backends" / name


def _backend_config_path(name: str) -> Path:
    return _backend_dir(name) / "backend.yaml"


def _backend_example_path(name: str) -> Path:
    return _backend_dir(name) / "backend.yaml.example"


def _schema_path(name: str) -> Path:
    return _backend_dir(name) / "schema.yaml"


def _ensure_backend_config_exists(name: str) -> None:
    """Copy backend.yaml.example to backend.yaml if the latter is missing."""
    config_path = _backend_config_path(name)
    if config_path.exists():
        return
    example_path = _backend_example_path(name)
    if not example_path.exists():
        raise BackendConfigError(
            name,
            [],
            f"Backend {name!r} has no config template at {example_path}",
        )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")


def load_backend_config(name: str) -> dict[str, Any]:
    """Load ``backends/<name>/backend.yaml``, creating it from example if needed."""
    _ensure_backend_config_exists(name)
    config_path = _backend_config_path(name)
    text = config_path.read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def _resolve_value(cfg: dict[str, Any], section: str, key: str, env_key: str) -> str:
    """Return a config value using the priority: direct > env > empty."""
    section_cfg = cfg.get(section, {}) if isinstance(cfg.get(section), dict) else {}
    direct = section_cfg.get(key, "")
    if direct:
        return str(direct)

    # Environment variable referenced by *_env field takes precedence over plain env.
    env_key_from_cfg = section_cfg.get(f"{key}_env", env_key)
    env_value = os.getenv(env_key_from_cfg, "")
    if env_value:
        return env_value

    # Fallback to the legacy env key if different.
    if env_key_from_cfg != env_key:
        env_value = os.getenv(env_key, "")
        if env_value:
            return env_value

    return ""


def resolve_env_overrides(cfg: dict[str, Any], backend: str) -> dict[str, Any]:
    """Return a copy of ``cfg`` with ``*_env`` fields resolved from environment."""
    cfg = _deep_copy(cfg)
    fields = _BACKEND_FIELDS.get(backend, [])
    for field in fields:
        value = _resolve_value(cfg, field.section, field.key, field.env_key)
        if value:
            cfg.setdefault(field.section, {})[field.key] = value
    return cfg


def _deep_copy(obj: Any) -> Any:
    """Minimal deep copy for dict/list primitives."""
    if isinstance(obj, dict):
        return {k: _deep_copy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_copy(v) for v in obj]
    return obj


def validate_backend(cfg: dict[str, Any], backend: str) -> list[dict[str, Any]]:
    """Return a list of missing required fields."""
    missing: list[dict[str, Any]] = []
    fields = _BACKEND_FIELDS.get(backend, [])
    for field in fields:
        if not field.required:
            continue
        value = _resolve_value(cfg, field.section, field.key, field.env_key)
        if not value:
            missing.append(
                {
                    "section": field.section,
                    "key": field.key,
                    "env_key": field.env_key,
                    "description": field.description,
                    "sensitive": field.sensitive,
                }
            )
    return missing


def _prompt_choice(prompt: str, choices: list[str], default: str = "") -> str:
    """Prompt user until one of ``choices`` is entered (case-insensitive)."""
    lower_to_orig = {c.lower(): c for c in choices}
    if default:
        prompt = f"{prompt} [{default}]: "
    else:
        prompt = f"{prompt} ({'/'.join(choices)}): "
    while True:
        try:
            answer = input(prompt).strip()
        except EOFError:
            raise
        if not answer and default:
            return default
        if answer.lower() in lower_to_orig:
            return lower_to_orig[answer.lower()]
        print(f"  Please enter one of: {', '.join(choices)}")


def _prompt_value(description: str, sensitive: bool = True) -> str:
    """Prompt user for a single value."""
    prompt = f"{description}: "
    while True:
        try:
            if sensitive:
                import getpass

                value = getpass.getpass(prompt).strip()
            else:
                value = input(prompt).strip()
        except EOFError:
            raise
        if value:
            return value
        print("  Value cannot be empty, please try again.")


def prompt_user_for_backend(
    backend: str, missing: list[dict[str, Any]]
) -> tuple[dict[str, str], bool]:
    """Interactively collect missing backend values.

    Returns ``(answers, save_to_env)`` where ``answers`` maps ``env_key`` to value.
    """
    print(f"\nBackend {backend!r} needs configuration:\n", file=sys.stderr)
    for item in missing:
        print(f"  - {item['description']} ({item['env_key']})", file=sys.stderr)
    print("", file=sys.stderr)

    answers: dict[str, str] = {}
    for item in missing:
        answers[item["env_key"]] = _prompt_value(
            item["description"], sensitive=item.get("sensitive", True)
        )

    save_to_env = (
        _prompt_choice(
            "Save these values to .env (recommended) or backend.yaml?",
            [".env", "backend.yaml"],
            default=".env",
        )
        == ".env"
    )

    return answers, save_to_env


def _migrate_legacy_notion(cfg: dict[str, Any]) -> dict[str, Any]:
    """Migrate old config.yaml notion block into backend config shape."""
    legacy = load_config().get("notion", {})
    if not legacy:
        return cfg
    cfg = _deep_copy(cfg)
    props = legacy.get("properties", {})
    if props:
        cfg.setdefault("mapping", {})["properties"] = props
    status_type = legacy.get("status_type", "")
    if status_type:
        cfg.setdefault("mapping", {})["status_type"] = status_type
    return cfg


def _migrate_legacy_lark(cfg: dict[str, Any]) -> dict[str, Any]:
    """Migrate old config.yaml lark block into backend config shape."""
    legacy = load_config().get("lark", {})
    if not legacy:
        return cfg
    cfg = _deep_copy(cfg)
    identity = legacy.get("identity", "")
    if identity:
        cfg.setdefault("auth", {})["identity"] = identity
    parent_folder_token = legacy.get("parent_folder_token", "")
    if parent_folder_token:
        cfg.setdefault("storage", {})["parent_token"] = parent_folder_token
    return cfg


def _migrate_legacy_config(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Apply one-time migration from old config.yaml blocks if present."""
    if name == "notion":
        return _migrate_legacy_notion(cfg)
    if name == "lark":
        return _migrate_legacy_lark(cfg)
    return cfg


def _save_backend_config(name: str, cfg: dict[str, Any]) -> None:
    """Write backend config to ``backends/<name>/backend.yaml``."""
    config_path = _backend_config_path(name)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def save_backend_values(
    backend: str,
    answers: dict[str, str],
    save_to_env: bool,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Persist collected values to .env or backend.yaml and return updated cfg."""
    if save_to_env:
        env_path = find_project_root() / ".env"
        write_env_values(env_path, answers)
        # Re-resolve so the returned cfg contains the new values.
        return resolve_env_overrides(cfg, backend)

    # Save direct values into backend.yaml.
    cfg = _deep_copy(cfg)
    fields = _BACKEND_FIELDS.get(backend, [])
    for field in fields:
        value = answers.get(field.env_key, "")
        if value:
            cfg.setdefault(field.section, {})[field.key] = value
    _save_backend_config(backend, cfg)
    return cfg


def _load_schema_properties(backend: str) -> dict[str, str]:
    """Load fallback property names from backends/<name>/schema.yaml if present."""
    schema_path = _schema_path(backend)
    if not schema_path.exists():
        return {}
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
    # Notion schema nests under a top-level key; Lark schema is flat.
    if backend == "notion":
        return schema.get("notion", {}).get("properties", {})
    return schema.get("properties", {})


def _schema_status_defaults(backend: str) -> dict[str, str]:
    """Return status_type / default_status defaults from schema.yaml."""
    schema_path = _schema_path(backend)
    if not schema_path.exists():
        return {}
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
    if backend == "notion":
        notion_cfg = schema.get("notion", {})
        return {
            "status_type": notion_cfg.get("status_type", "checkbox"),
            "default_status": notion_cfg.get("default_status", "Unread"),
        }
    return {}


def load_notion_config(interactive: bool = True) -> dict[str, Any]:
    """Load fully resolved Notion backend config with schema fallbacks."""
    cfg = require_backend_config("notion", interactive=interactive)
    auth = cfg.get("auth", {})
    mapping = cfg.get("mapping", {})
    schema_props = _load_schema_properties("notion")
    schema_status = _schema_status_defaults("notion")
    user_props = mapping.get("properties", {})

    return {
        "token": auth.get("token", ""),
        "database_id": auth.get("database_id", ""),
        "parent_page_id": auth.get("parent_page_id", ""),
        "properties": {**schema_props, **user_props},
        "status_type": mapping.get(
            "status_type", schema_status.get("status_type", "checkbox")
        ),
        "default_status": mapping.get(
            "default_status", schema_status.get("default_status", "Unread")
        ),
    }


def load_lark_config(interactive: bool = True) -> dict[str, Any]:
    """Load fully resolved Lark backend config."""
    cfg = require_backend_config("lark", interactive=interactive)
    auth = cfg.get("auth", {})
    storage = cfg.get("storage", {})
    knowledge_base = cfg.get("knowledge_base", {})
    fmt = cfg.get("format", {})
    return {
        "identity": auth.get("identity", "user"),
        "profile": auth.get("profile", ""),
        "storage_type": storage.get("type", "docx"),
        "parent_token": storage.get("parent_token", ""),
        "folder_token": storage.get("folder_token", ""),
        "wiki_space_id": knowledge_base.get("wiki_space_id", ""),
        "wiki_parent_node_token": knowledge_base.get("wiki_parent_node_token", ""),
        "include_captions": fmt.get("include_captions", True),
        "image_position": fmt.get("image_position", "inline"),
    }


def require_backend_config(
    backend: str,
    *,
    interactive: bool = True,
) -> dict[str, Any]:
    """Load and validate a backend config, optionally prompting the user.

    Args:
        backend: backend name (e.g. ``notion``, ``lark``).
        interactive: if True and values are missing, prompt via ``input()``
            when stdin is available. Otherwise raise ``BackendConfigError``.

    Returns:
        Fully resolved backend configuration dict.

    Raises:
        BackendConfigError: if configuration is incomplete and cannot be
            resolved interactively.
    """
    load_config()  # ensure .env is loaded
    cfg = load_backend_config(backend)
    cfg = _migrate_legacy_config(backend, cfg)
    cfg = resolve_env_overrides(cfg, backend)
    missing = validate_backend(cfg, backend)

    if not missing:
        return cfg

    if not interactive or not sys.stdin.isatty():
        raise BackendConfigError(backend, missing)

    try:
        answers, save_to_env = prompt_user_for_backend(backend, missing)
    except EOFError:
        raise BackendConfigError(backend, missing)

    cfg = save_backend_values(backend, answers, save_to_env, cfg)
    missing = validate_backend(cfg, backend)
    if missing:
        raise BackendConfigError(backend, missing)
    return cfg
