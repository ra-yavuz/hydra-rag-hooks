"""crh refresher - auto-refresh daemon.

Foreground entrypoint (`crh refresher run`) plus systemctl --user
wrappers (start/stop/status). The daemon is designed to be off by
default; users opt in per-host (systemctl --user enable) AND per
project (.claude-rag-index/.auto-refresh marker file).

Resource hygiene:
- The systemd user unit runs us at Nice=19, IOSchedulingClass=idle,
  CPUSchedulingPolicy=idle, so we naturally yield to the user's
  foreground work.
- Coalesce: after detecting a change, wait COALESCE_QUIET_SECONDS
  of no further changes before kicking a refresh. A `git pull`
  produces thousands of mtime changes; we want one refresh, not
  thousands.
- Throttle: never refresh the same project more than once per
  REFRESH_FLOOR_SECONDS regardless of how many events fire.
- Skip on busy system: if loadavg-1m > LOAD_THRESHOLD or laptop is
  on battery below LOW_BATTERY_PCT, skip this round.

Why polling instead of inotify (for v0.4.0):
- Stdlib-only; no extra dep on python3-inotify or inotify_simple.
- Polling at 30s adds zero kernel-watch state for monstrous trees.
- The 30s-60s coalesce + refresh-floor windows hide the polling
  latency entirely; auto-refresh is "every few minutes after a
  change", not "instant on save".

A future v0.5 can swap the polling loop for inotify with the same
coalesce/throttle/skip logic on top.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .. import paths, progress as progress_mod, registry, runner
from . import _common


_MARKER = ".auto-refresh"
_UNIT = "hydra-rag-hooks-refresher.service"

# Loop tuning
POLL_INTERVAL_SECONDS = 30        # how often we re-scan watched projects
COALESCE_QUIET_SECONDS = 60       # wait this long after the last change before refreshing
REFRESH_FLOOR_SECONDS = 300       # hard floor between refreshes per project
LOAD_THRESHOLD = 1.5              # skip refresh if loadavg-1m above this
LOW_BATTERY_PCT = 30              # skip refresh on battery below this
SCAN_DEPTH_LIMIT = 6              # how deep into project tree the change-detector descends
SCAN_TIMEOUT_SECONDS = 5          # cap per-project scan walltime so a huge tree doesn't stall


def _is_busy() -> tuple[bool, str]:
    """Return (busy, reason) so we can log why a tick skipped."""
    try:
        load1, _, _ = os.getloadavg()
    except OSError:
        load1 = 0.0
    if load1 > LOAD_THRESHOLD:
        return True, f"loadavg {load1:.2f} > {LOAD_THRESHOLD}"

    # Check battery (best-effort; absent on desktops).
    bat_dir = Path("/sys/class/power_supply")
    if bat_dir.is_dir():
        for d in bat_dir.iterdir():
            if not d.name.startswith("BAT"):
                continue
            status_p = d / "status"
            cap_p = d / "capacity"
            try:
                status = status_p.read_text().strip() if status_p.exists() else ""
                cap = int(cap_p.read_text().strip()) if cap_p.exists() else 100
            except (OSError, ValueError):
                continue
            if status == "Discharging" and cap < LOW_BATTERY_PCT:
                return True, f"on battery, {cap}% (< {LOW_BATTERY_PCT}%)"
    return False, ""


def _max_mtime(scope: Path, deadline: float) -> float:
    """Return the maximum mtime across files in `scope` (cheap-ish recursive walk).

    Bounded by SCAN_DEPTH_LIMIT and a wall-clock deadline so a
    pathological 100k-file tree doesn't lock up the daemon. Skips the
    .claude-rag-index/ dir itself (its files change as we write the
    index, which would be a feedback loop).
    """
    best = 0.0

    def walk(d: Path, depth: int) -> bool:
        """Returns True if we should keep walking; False if deadline hit."""
        nonlocal best
        if time.monotonic() > deadline:
            return False
        if depth > SCAN_DEPTH_LIMIT:
            return True
        try:
            entries = list(os.scandir(d))
        except OSError:
            return True
        for ent in entries:
            if time.monotonic() > deadline:
                return False
            name = ent.name
            if name in (".claude-rag-index", ".hydra-index", ".git", "node_modules", ".venv", "__pycache__"):
                continue
            try:
                st = ent.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_mtime > best:
                best = st.st_mtime
            if ent.is_dir(follow_symlinks=False):
                if not walk(Path(ent.path), depth + 1):
                    return False
        return True

    walk(scope, 0)
    return best


class _ProjectState:
    __slots__ = (
        "scope", "last_seen_mtime", "last_change_at",
        "last_refresh_at", "pending_change",
    )

    def __init__(self, scope: Path):
        self.scope = scope
        self.last_seen_mtime = 0.0
        self.last_change_at = 0.0
        self.last_refresh_at = 0.0
        self.pending_change = False


def _watched_projects() -> list[Path]:
    """Every registered store with a `.auto-refresh` marker."""
    out = []
    for entry in registry.load():
        scope = Path(entry.path).resolve()
        marker = scope / paths.INDEX_DIR_NAME / _MARKER
        if marker.is_file():
            out.append(scope)
    return out


def run_run(args) -> int:
    """Foreground daemon entrypoint."""
    log_path = paths.cache_dir() / "refresher.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} {msg}\n"
        try:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
        # Also stdout so journalctl sees it under systemd.
        sys.stdout.write(line)
        sys.stdout.flush()

    log("refresher: starting")

    # SIGHUP -> rebuild watched-projects list. SIGTERM -> clean exit.
    rebuild = {"flag": True}
    stop = {"flag": False}

    def on_hup(_s, _f):
        rebuild["flag"] = True
        log("refresher: SIGHUP received, rebuilding project list")

    def on_term(_s, _f):
        stop["flag"] = True
        log("refresher: SIGTERM received, exiting")

    signal.signal(signal.SIGHUP, on_hup)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    states: dict[str, _ProjectState] = {}

    while not stop["flag"]:
        if rebuild["flag"]:
            rebuild["flag"] = False
            current = {str(p): p for p in _watched_projects()}
            # Drop states for projects no longer opted in.
            for key in list(states):
                if key not in current:
                    log(f"refresher: drop watch on {key}")
                    states.pop(key)
            for key, scope in current.items():
                if key not in states:
                    log(f"refresher: watch {key}")
                    states[key] = _ProjectState(scope)
                    # Seed mtime so we don't fire on first scan from "0".
                    deadline = time.monotonic() + SCAN_TIMEOUT_SECONDS
                    states[key].last_seen_mtime = _max_mtime(scope, deadline)

        if not states:
            # Nothing to watch; sleep and re-check.
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        now = time.time()
        for key, st in states.items():
            deadline = time.monotonic() + SCAN_TIMEOUT_SECONDS
            current_max = _max_mtime(st.scope, deadline)
            if current_max > st.last_seen_mtime:
                st.last_seen_mtime = current_max
                st.last_change_at = now
                st.pending_change = True

        # Decide which projects are due for a refresh.
        for key, st in states.items():
            if not st.pending_change:
                continue
            quiet_for = now - st.last_change_at
            if quiet_for < COALESCE_QUIET_SECONDS:
                continue
            since_last_refresh = now - st.last_refresh_at
            if since_last_refresh < REFRESH_FLOOR_SECONDS:
                continue
            busy, reason = _is_busy()
            if busy:
                log(f"refresher: skip {key}, {reason}")
                continue

            # Don't pile on if a job is already running for this scope.
            index_dir = st.scope / paths.INDEX_DIR_NAME
            if progress_mod.is_active(index_dir):
                log(f"refresher: {key} already has an active job")
                st.last_refresh_at = now
                st.pending_change = False
                continue

            # Kick off the refresh through the same fork-detach path
            # the hook uses. The runner handles all the details
            # (warm embedder daemon, manifest checkpoints, error
            # persistence). We just initiate.
            try:
                runner.fork_detach_index(st.scope, kind="refreshing")
                st.last_refresh_at = now
                st.pending_change = False
                log(f"refresher: kicked refresh for {key}")
            except Exception as e:  # noqa: BLE001
                log(f"refresher: failed to kick refresh for {key}: {e}")
                # Don't reset pending_change; we'll retry next tick.

        time.sleep(POLL_INTERVAL_SECONDS)

    log("refresher: stopped")
    return 0


def _systemctl(*sub_args: str) -> int:
    if not shutil.which("systemctl"):
        _common.stderr(
            "crh refresher: systemctl not found. Use `crh refresher run` "
            "directly under your own process supervisor (cron, supervisord, etc)."
        )
        return 1
    try:
        return subprocess.run(["systemctl", "--user", *sub_args]).returncode
    except OSError as e:
        _common.stderr(f"crh refresher: systemctl failed: {e}")
        return 1


def run_start(args) -> int:
    rc = _systemctl("enable", "--now", _UNIT)
    if rc == 0:
        print(f"started {_UNIT} (will auto-start on next login)")
        print("Disable with `crh refresher stop`. Per-project opt-in: `crh auto on`.")
    return rc


def run_stop(args) -> int:
    rc = _systemctl("disable", "--now", _UNIT)
    if rc == 0:
        print(f"stopped {_UNIT}")
    return rc


def run_status(args) -> int:
    if not shutil.which("systemctl"):
        if args.json:
            _common.emit_json({"systemd": "unavailable", "watched": []})
        else:
            print("systemd unavailable")
            for p in _watched_projects():
                print(f"  watched: {p}")
        return 0
    out = subprocess.run(
        ["systemctl", "--user", "is-active", _UNIT],
        capture_output=True, text=True,
    )
    state = out.stdout.strip() or "(unknown)"
    watched = [str(p) for p in _watched_projects()]
    if args.json:
        _common.emit_json({"systemd_state": state, "watched": watched})
        return 0
    print(f"systemd state: {state}")
    if not watched:
        print("  no projects opted in (drop a `.claude-rag-index/.auto-refresh` "
              "or run `crh auto on <path>`)")
    else:
        print("  watched projects:")
        for p in watched:
            print(f"    - {p}")
    return 0
