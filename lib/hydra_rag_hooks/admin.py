"""Tiny admin module called from apt postinst / postrm.

This is the only piece of the package that's allowed to write to system
settings files. The hook itself never touches them.

Two operations:

  install <command>     merge a UserPromptSubmit hook entry into a
                        Claude Code settings.json (default:
                        /etc/claude-code/managed-settings.json), creating
                        it if missing. Idempotent.
  uninstall             remove our hook entry from the same file. Leaves
                        any other entries alone. If the file ends up
                        empty, leaves it as `{}` (does not delete it).

The hook entry uses Claude Code's real schema (verified against
docs.claude.com / code.claude.com hook docs):

    {
      "hooks": {
        "UserPromptSubmit": [
          {
            "hooks": [
              {"type": "command", "command": "<absolute path to hook>"}
            ]
          }
        ]
      }
    }

We tag our entry by command-equality on the absolute path; that's how
uninstall finds it. No fake `name` field (Claude Code ignores unknown
fields anyway, but we no longer rely on them).

Fallback: if /etc/claude-code/ is unwritable, the caller can pass
~/.claude/settings.json; the schema is the same.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path
from typing import Any


DEFAULT_HOOK_COMMAND = "/usr/lib/hydra-rag-hooks/hydra-rag-hooks-claude-hook"
SYSTEM_SETTINGS = Path("/etc/claude-code/managed-settings.json")
USER_SETTINGS = Path.home() / ".claude" / "settings.json"


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    bak = path.with_suffix(path.suffix + f".bak.{ts}")
    shutil.copy2(path, bak)
    return bak


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        return {}
    return json.loads(text)


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _has_our_entry(matchers: list[Any], command: str) -> bool:
    for m in matchers:
        if not isinstance(m, dict):
            continue
        for h in m.get("hooks") or []:
            if isinstance(h, dict) and h.get("command") == command:
                return True
    return False


def install(command: str = DEFAULT_HOOK_COMMAND,
            settings_path: Path = SYSTEM_SETTINGS) -> tuple[Path, Path | None, bool]:
    data = _load(settings_path)
    hooks_root = data.setdefault("hooks", {})
    if not isinstance(hooks_root, dict):
        raise RuntimeError(
            f"{settings_path}: 'hooks' is not an object; refusing to modify."
        )
    matchers = hooks_root.setdefault("UserPromptSubmit", [])
    if not isinstance(matchers, list):
        raise RuntimeError(
            f"{settings_path}: 'hooks.UserPromptSubmit' is not an array; refusing to modify."
        )

    if _has_our_entry(matchers, command):
        return settings_path, None, False

    matchers.append({
        "hooks": [
            {"type": "command", "command": command},
        ],
    })
    bak = _backup(settings_path)
    _save(settings_path, data)
    return settings_path, bak, True


def uninstall(command: str = DEFAULT_HOOK_COMMAND,
              settings_path: Path = SYSTEM_SETTINGS) -> tuple[Path, Path | None, bool]:
    if not settings_path.exists():
        return settings_path, None, False
    try:
        data = _load(settings_path)
    except json.JSONDecodeError:
        return settings_path, None, False

    hooks_root = data.get("hooks") or {}
    if not isinstance(hooks_root, dict):
        return settings_path, None, False
    matchers = hooks_root.get("UserPromptSubmit")
    if not isinstance(matchers, list):
        return settings_path, None, False

    changed = False
    new_matchers = []
    for m in matchers:
        if not isinstance(m, dict):
            new_matchers.append(m)
            continue
        old_inner = m.get("hooks") or []
        new_inner = [
            h for h in old_inner
            if not (isinstance(h, dict) and h.get("command") == command)
        ]
        if len(new_inner) != len(old_inner):
            changed = True
        if new_inner:
            m["hooks"] = new_inner
            new_matchers.append(m)
        # else: drop this matcher entry entirely

    if not changed:
        return settings_path, None, False

    if new_matchers:
        hooks_root["UserPromptSubmit"] = new_matchers
    else:
        hooks_root.pop("UserPromptSubmit", None)
    if not hooks_root:
        data.pop("hooks", None)

    bak = _backup(settings_path)
    _save(settings_path, data)
    return settings_path, bak, True


def is_installed(command: str = DEFAULT_HOOK_COMMAND,
                 settings_path: Path = SYSTEM_SETTINGS) -> bool:
    if not settings_path.exists():
        return False
    try:
        data = _load(settings_path)
    except json.JSONDecodeError:
        return False
    matchers = (data.get("hooks") or {}).get("UserPromptSubmit") or []
    if not isinstance(matchers, list):
        return False
    return _has_our_entry(matchers, command)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="hydra-rag-hooks-admin",
        description=(
            "Internal admin tool used by the apt postinst / postrm. "
            "Not intended to be called by end users."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("install", help="Add the hook entry to a Claude Code settings file")
    sp.add_argument("--command", default=DEFAULT_HOOK_COMMAND)
    sp.add_argument("--settings", default=str(SYSTEM_SETTINGS))
    sp = sub.add_parser("uninstall", help="Remove the hook entry from a Claude Code settings file")
    sp.add_argument("--command", default=DEFAULT_HOOK_COMMAND)
    sp.add_argument("--settings", default=str(SYSTEM_SETTINGS))
    sp = sub.add_parser("status", help="Print whether our hook entry is present")
    sp.add_argument("--command", default=DEFAULT_HOOK_COMMAND)
    sp.add_argument("--settings", default=str(SYSTEM_SETTINGS))

    args = p.parse_args(argv)
    settings_path = Path(args.settings)

    if args.cmd == "install":
        path, bak, changed = install(command=args.command, settings_path=settings_path)
        if not changed:
            print(f"already installed in {path}")
            return 0
        print(f"installed hook entry in {path}")
        if bak:
            print(f"backup: {bak}")
        return 0

    if args.cmd == "uninstall":
        path, bak, changed = uninstall(command=args.command, settings_path=settings_path)
        if not changed:
            print(f"no hydra-rag-hooks entry found in {path}")
            return 0
        print(f"removed hook entry from {path}")
        if bak:
            print(f"backup: {bak}")
        return 0

    if args.cmd == "status":
        if is_installed(command=args.command, settings_path=settings_path):
            print(f"installed in {settings_path}")
            return 0
        print(f"not installed in {settings_path}", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
