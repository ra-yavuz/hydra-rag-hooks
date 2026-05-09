"""Per-index progress + refresh-throttle state.

Two tiny files inside `<scope>/.claude-rag-index/`:

    .progress      JSON: {state, started_at, files_done, files_total, pid, ...}
    .last_refresh  unix timestamp of the most recent refresh attempt

Used by:
    - the indexer (writes .progress as it walks; clears it on completion)
    - the hook (reads .progress to compose the stderr nudge; reads
      .last_refresh to decide whether to fork a background refresh)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

PROGRESS_FILE = ".progress"
LAST_REFRESH_FILE = ".last_refresh"
LAST_RUN_FILE = ".last_run.json"
REFRESH_INTERVAL_SECONDS = 300  # 5 minutes


@dataclass
class LastRun:
    """Stats from the most recent successful indexing/refresh.

    Used by the bare-`rag` status command to print concrete numbers
    ("4231 chunks across 312 files, last refreshed 8 min ago") instead
    of just "ready".
    """

    finished_at: float = 0.0
    elapsed_seconds: float = 0.0
    kind: str = ""           # "indexing" | "refreshing"
    files_total: int = 0
    files_indexed: int = 0
    files_pruned: int = 0
    chunks_added: int = 0


@dataclass
class Progress:
    state: str = "idle"       # idle | indexing | refreshing | error
    started_at: float = 0.0
    files_done: int = 0
    files_total: int = 0
    pid: int = 0
    message: str = ""

    def as_human(self) -> str:
        if self.state in {"indexing", "refreshing"}:
            elapsed = max(0, int(time.time() - self.started_at))
            verb = "indexing" if self.state == "indexing" else "refreshing"
            if self.files_total > 0:
                return f"hydra-rag-hooks: {verb}, {self.files_done}/{self.files_total} files ({elapsed}s)"
            return f"hydra-rag-hooks: {verb}, {self.files_done} files so far ({elapsed}s)"
        if self.state == "error":
            return f"hydra-rag-hooks: indexing failed: {self.message}"
        return ""


def _progress_path(index_dir: Path) -> Path:
    return index_dir / PROGRESS_FILE


def _last_refresh_path(index_dir: Path) -> Path:
    return index_dir / LAST_REFRESH_FILE


def read(index_dir: Path) -> Progress:
    p = _progress_path(index_dir)
    if not p.exists():
        return Progress()
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return Progress(**{k: v for k, v in data.items() if k in Progress.__annotations__})
    except (OSError, json.JSONDecodeError, TypeError):
        return Progress()


def write(index_dir: Path, prog: Progress) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    p = _progress_path(index_dir)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(asdict(prog), f)
    tmp.replace(p)


def clear(index_dir: Path) -> None:
    p = _progress_path(index_dir)
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def is_active(index_dir: Path) -> bool:
    """A live indexing job is in progress if .progress says so AND its pid is alive."""
    prog = read(index_dir)
    if prog.state not in {"indexing", "refreshing"}:
        return False
    if prog.pid <= 0:
        return False
    try:
        os.kill(prog.pid, 0)
    except OSError:
        return False
    return True


def mark_refresh(index_dir: Path) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    _last_refresh_path(index_dir).write_text(f"{int(time.time())}\n")


def needs_refresh(index_dir: Path, interval: float = REFRESH_INTERVAL_SECONDS) -> bool:
    """Has the index gone too long without a refresh attempt?"""
    p = _last_refresh_path(index_dir)
    if not p.exists():
        return True
    try:
        last = float(p.read_text().strip())
    except (OSError, ValueError):
        return True
    return (time.time() - last) >= interval


def _last_run_path(index_dir: Path) -> Path:
    return index_dir / LAST_RUN_FILE


def write_last_run(index_dir: Path, run: LastRun) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    p = _last_run_path(index_dir)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(asdict(run), f)
    tmp.replace(p)


def read_last_run(index_dir: Path) -> LastRun | None:
    p = _last_run_path(index_dir)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return LastRun(**{k: v for k, v in data.items() if k in LastRun.__annotations__})
    except (OSError, json.JSONDecodeError, TypeError):
        return None
