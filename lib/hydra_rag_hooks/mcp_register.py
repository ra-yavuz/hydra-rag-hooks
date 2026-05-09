"""Per-user MCP server self-registration in ~/.claude.json.

Background: Claude Code reads MCP server configs for user-scope from
~/.claude.json (per the docs, not from the package's managed-settings
file). The package's apt postinst runs as root and cannot reliably
write to every user's home directory, so registration cannot happen
there. Instead the hook (which runs as the target user when Claude
Code invokes it) registers the MCP entry idempotently on every run.

The work is cheap: read JSON, check if our entry already exists with
the right command and disabled-state, return if so. Touch ~/.claude.json
only when something changed. Write atomically (tmp file + rename) to
avoid corrupting the user's file mid-write.

The entry shape we write is the standard stdio MCP server form:

    "mcpServers": {
      "claude-rag": {
        "type": "stdio",
        "command": "/usr/lib/hydra-rag-hooks/hydra-rag-mcp",
        "env": { "CLAUDE_RAG_MCP_DISABLED": "1" }   # only if toggled off
      }
    }

When the user toggles MCP off (`crh mcp off`), we add the disable env
var rather than deleting the entry. Claude Code then spawns the
process, which exits immediately, and shows the server as inactive in
`/mcp`. This keeps the user's mental model simple: the entry is
always there; flipping the toggle changes whether the tools are live.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


SERVER_NAME = "claude-rag"
DEFAULT_COMMAND = "/usr/lib/hydra-rag-hooks/hydra-rag-mcp"


def _claude_json() -> Path:
    return Path.home() / ".claude.json"


class ParseError(Exception):
    """Raised by _read when ~/.claude.json exists but cannot be parsed.

    The hook treats this as fatal-for-this-call and skips registration:
    overwriting a malformed user-owned file would destroy unrelated
    config (other MCP servers, project trust state, OAuth session).
    """


def _read(path: Path) -> dict[str, Any]:
    """Read ~/.claude.json. Returns an empty dict only when the file is
    genuinely empty or missing. Raises ParseError when the file exists
    with content we cannot parse, so callers know to back off rather
    than overwrite."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ParseError(f"could not read {path}: {e}") from e
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ParseError(f"{path} is not a JSON object")
    return data


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp-claude-rag")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def desired_entry(command: str, disabled: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "stdio", "command": command}
    if disabled:
        entry["env"] = {"CLAUDE_RAG_MCP_DISABLED": "1"}
    return entry


def ensure_registered(command: str = DEFAULT_COMMAND, disabled: bool = False,
                      claude_json: Path | None = None) -> bool:
    """Idempotently sync our MCP server entry.

    Returns True if the file was modified, False if it already matched
    the desired state OR if the existing file could not be parsed (we
    bail rather than overwrite user data).
    """
    path = claude_json or _claude_json()
    try:
        data = _read(path)
    except ParseError:
        # Refuse to overwrite a malformed user file: it could be a
        # half-saved edit, a trailing comma, a manual customisation
        # we don't understand. The hook is fail-soft; the worst case
        # is that the MCP server is not registered until the user
        # fixes their JSON. We never destroy their data.
        return False
    mcp = data.get("mcpServers")
    if not isinstance(mcp, dict):
        mcp = {}
    desired = desired_entry(command, disabled)
    current = mcp.get(SERVER_NAME)
    if isinstance(current, dict) and _entries_match(current, desired):
        return False
    mcp[SERVER_NAME] = desired
    data["mcpServers"] = mcp
    try:
        _write_atomic(path, data)
    except OSError:
        return False
    return True


def _entries_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("type") != b.get("type"):
        return False
    if a.get("command") != b.get("command"):
        return False
    a_env = a.get("env") or {}
    b_env = b.get("env") or {}
    return a_env.get("CLAUDE_RAG_MCP_DISABLED") == b_env.get("CLAUDE_RAG_MCP_DISABLED")


def unregister(claude_json: Path | None = None) -> bool:
    """Remove our entry from ~/.claude.json. Returns True if changed.

    Refuses to operate on a malformed file (same fail-safe rule as
    ensure_registered: we never overwrite user data we cannot parse).
    """
    path = claude_json or _claude_json()
    try:
        data = _read(path)
    except ParseError:
        return False
    mcp = data.get("mcpServers")
    if not isinstance(mcp, dict) or SERVER_NAME not in mcp:
        return False
    del mcp[SERVER_NAME]
    if not mcp:
        data.pop("mcpServers", None)
    try:
        _write_atomic(path, data)
    except OSError:
        return False
    return True


def is_registered(claude_json: Path | None = None) -> bool:
    path = claude_json or _claude_json()
    try:
        data = _read(path)
    except ParseError:
        return False
    mcp = data.get("mcpServers")
    if not isinstance(mcp, dict):
        return False
    return SERVER_NAME in mcp


# ---------------------------------------------------------------------------
# /rag slash command installer
# ---------------------------------------------------------------------------

# We ship the markdown source alongside the package and copy it to
# ~/.claude/commands/rag.md on first hook run. Same "self-install on
# first run" pattern as the MCP registration above. Idempotent: only
# touches the file when the shipped content is newer.

_SLASH_COMMAND_FILENAME = "rag-toggle.md"


def _shipped_command_path() -> Path | None:
    """Return the absolute path to the rag.md shipped with the package,
    or None if it cannot be found."""
    candidates = [
        Path("/usr/lib/hydra-rag-hooks/commands") / _SLASH_COMMAND_FILENAME,
        Path("/usr/share/hydra-rag-hooks/commands") / _SLASH_COMMAND_FILENAME,
        # Source-checkout fallback: <repo>/commands/rag.md
        Path(__file__).resolve().parent.parent.parent / "commands" / _SLASH_COMMAND_FILENAME,
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


# A marker we add to our shipped file so we can recognise an existing
# install on disk as "ours" and update it across versions, vs. a
# user-owned file at the same path that we must leave alone. Any
# user who edits the file is welcome to keep the marker (we'll
# overwrite their changes on the next package update) or remove it
# (we'll then leave their file alone forever). The marker doubles as
# a clear "this came from hydra-rag-hooks" attribution.
_SLASH_COMMAND_MARKER = (
    "<!-- hydra-rag-hooks: shipped slash command. Remove this comment "
    "to keep your local edits across upgrades. -->"
)


def ensure_slash_command(target_dir: Path | None = None) -> bool:
    """Install ~/.claude/commands/rag-toggle.md if missing or if the
    on-disk copy is one we shipped (carries our marker). Never
    overwrites a user-owned file at the same path.

    Returns True if the file was changed.
    """
    src = _shipped_command_path()
    if src is None:
        return False
    target_dir = target_dir or (Path.home() / ".claude" / "commands")
    target = target_dir / _SLASH_COMMAND_FILENAME
    try:
        shipped_body = src.read_text(encoding="utf-8")
    except OSError:
        return False
    new_content = _SLASH_COMMAND_MARKER + "\n" + shipped_body
    if target.exists():
        try:
            current = target.read_text(encoding="utf-8")
        except OSError:
            return False
        if current == new_content:
            # Already up to date.
            return False
        if _SLASH_COMMAND_MARKER not in current:
            # User-owned file at the same path; never overwrite.
            return False
        # Else: previous version of our shipped file. Safe to update.
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(new_content, encoding="utf-8")
    except OSError:
        return False
    return True
