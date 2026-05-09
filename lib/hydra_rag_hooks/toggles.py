"""Per-user runtime toggles.

Two booleans live here, both stored at $XDG_STATE_HOME/hydra-rag-hooks/
toggles.json so they persist across sessions:

- `auto_rag`: when true, the hook treats every prompt as if the user
  had typed `rag <prompt>`. Default false. Toggled via `crh rag toggle`
  or the bundled `/rag` slash command.
- `mcp_enabled`: when false, the MCP server entry in the user's
  ~/.claude.json runs with CLAUDE_RAG_MCP_DISABLED=1 and exits
  immediately, so Claude Code does not see any rag_* tools. Default
  true. Toggled via `crh mcp toggle`.

Module surface is small on purpose: read/write and a couple of
dotted-key getters. The on-disk file is JSON for easy human inspection
and edit-with-any-editor recovery.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import paths


_DEFAULTS: dict[str, Any] = {
    "auto_rag": False,
    "mcp_enabled": True,
}


def _file() -> Path:
    return paths.state_dir() / "toggles.json"


def load() -> dict[str, Any]:
    p = _file()
    if not p.exists():
        return dict(_DEFAULTS)
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULTS)
    if not isinstance(data, dict):
        return dict(_DEFAULTS)
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in data.items() if k in _DEFAULTS})
    return out


def save(data: dict[str, Any]) -> None:
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True)
    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in _DEFAULTS})
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")
    tmp.replace(p)


def get(key: str) -> Any:
    return load().get(key, _DEFAULTS.get(key))


def set_value(key: str, value: Any) -> dict[str, Any]:
    if key not in _DEFAULTS:
        raise KeyError(f"unknown toggle: {key}")
    data = load()
    data[key] = value
    save(data)
    return data


def auto_rag_enabled() -> bool:
    return bool(get("auto_rag"))


def mcp_enabled() -> bool:
    return bool(get("mcp_enabled"))
