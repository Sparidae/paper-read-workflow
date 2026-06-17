# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Safe .env file reader/writer that preserves comments."""

from __future__ import annotations

from pathlib import Path


def read_env(env_path: Path) -> dict[str, str]:
    """Read key/value pairs from a .env-style file.

    Comments and blank lines are ignored. Values are taken literally after the
    first ``=`` on each non-comment line.
    """
    values: dict[str, str] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value
    return values


def write_env_values(env_path: Path, values: dict[str, str]) -> None:
    """Update or append values in ``env_path`` while keeping existing comments.

    If ``env_path`` does not exist, it is created.
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)

    existing_keys: set[str] = set()
    lines: list[str] = []

    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.rstrip("\n")
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in values:
                    # Preserve original indentation by replacing only the value part.
                    prefix = line[: line.index("=") + 1]
                    line = f"{prefix}{values[key]}"
                    existing_keys.add(key)
            lines.append(line)

    for key, value in values.items():
        if key not in existing_keys:
            lines.append(f"{key}={value}")

    # Ensure a single trailing newline.
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    env_path.write_text(text, encoding="utf-8")
