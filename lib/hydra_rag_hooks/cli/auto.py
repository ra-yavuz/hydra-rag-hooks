"""crh auto on / crh auto off - opt a project into the auto-refresh daemon.

The marker file `<scope>/.claude-rag-index/.auto-refresh` is the
unit of opt-in. The daemon reads this file's existence to decide
whether to watch a project. SIGHUP to the running daemon makes it
re-read the marker set immediately.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from .. import paths
from . import _common


_MARKER = ".auto-refresh"


def _resolve_indexed_scope(arg: str | None) -> Path | None:
    target = _common.resolve_path(arg)
    existing = paths.find_index(target)
    if existing is None:
        _common.stderr(
            f"crh auto: no index at or above {target}. "
            f"`crh index` first; then opt in with `crh auto on`."
        )
        return None
    return existing.parent


def _signal_daemon() -> None:
    """Try to SIGHUP the running refresher daemon so it picks up the
    marker change immediately. Best-effort; silent failure is fine."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value",
             "hydra-rag-hooks-refresher.service"],
            capture_output=True, text=True, timeout=2,
        )
        pid_str = (out.stdout or "").strip()
        if pid_str.isdigit() and int(pid_str) > 0:
            os.kill(int(pid_str), signal.SIGHUP)
    except (subprocess.SubprocessError, OSError):
        pass


def run_on(args) -> int:
    scope = _resolve_indexed_scope(args.path)
    if scope is None:
        return 1
    marker = scope / paths.INDEX_DIR_NAME / _MARKER
    if marker.exists():
        print(f"(auto-refresh already on for {scope})")
        return 0
    marker.write_text("")
    _signal_daemon()
    print(f"auto-refresh on for {scope}")
    print(
        "Note: the refresher daemon is off by default. Enable with "
        "`crh refresher start`."
    )
    return 0


def run_off(args) -> int:
    scope = _resolve_indexed_scope(args.path)
    if scope is None:
        return 1
    marker = scope / paths.INDEX_DIR_NAME / _MARKER
    if not marker.exists():
        print(f"(auto-refresh already off for {scope})")
        return 0
    marker.unlink()
    _signal_daemon()
    print(f"auto-refresh off for {scope}")
    return 0
