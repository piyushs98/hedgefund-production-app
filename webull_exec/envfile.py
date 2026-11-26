"""Load .env without printing it, and set EXECUTION_MODE=off in place."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path | None = None) -> None:
    """Set variables that are not already in the process environment."""
    env_path = path or Path(".env")
    if not env_path.exists():
        return
    text = env_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def read_mode(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key == "EXECUTION_MODE":
            return value.strip().strip("'\"").lower()
    return None


def set_mode_off(path: Path) -> None:
    """Replace or append EXECUTION_MODE=off. Other lines stay as they are."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    found = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = ""
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
        if key == "EXECUTION_MODE":
            out.append("EXECUTION_MODE=off")
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1] != "":
            out.append("")
        out.append("EXECUTION_MODE=off")
    body = "\n".join(out)
    if text.endswith("\n") or not text:
        body += "\n"
    path.write_text(body, encoding="utf-8")
