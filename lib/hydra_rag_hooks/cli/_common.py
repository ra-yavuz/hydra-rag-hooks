"""Shared helpers for crh subcommands.

Path resolution, scope finding, human-friendly formatters, and the
live progress renderer used by status --watch / index --watch.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .. import auto_index, paths, progress as progress_mod


def resolve_path(arg: str | None) -> Path:
    """Turn a CLI path argument into an absolute Path. None -> cwd."""
    return (Path(arg) if arg else Path.cwd()).resolve()


def find_scope(start: Path) -> Path | None:
    """Find the scope (project root) for a path.

    Prefers an existing `.claude-rag-index/` (or `.hydra-index/`) up
    the tree; falls back to project-marker walk-up. Returns None if
    nothing matches.
    """
    existing = paths.find_index(start)
    if existing is not None:
        return existing.parent
    return auto_index.find_project_root(start)


def index_dir_for(scope: Path) -> Path:
    return scope / paths.INDEX_DIR_NAME


def human_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{n} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n} TB"


def render_progress_line(
    scope: Path,
    prog: progress_mod.Progress,
    width: int = 40,
) -> str:
    """One-line live progress display.

    Format:
      [indexing] /path  1240/3815 (32%)  47s elapsed  ~3m left
      [████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░]
    """
    elapsed = max(0.0, time.time() - prog.started_at)
    total = max(1, prog.files_total or 1)
    done = max(0, min(prog.files_done or 0, total))
    pct = done / total * 100

    if done > 0 and elapsed > 0:
        rate = done / elapsed
        remaining = (total - done) / rate if rate > 0 else 0
        eta = f"~{human_duration(remaining)} left"
    else:
        eta = "ETA pending"

    bar_n = int((done / total) * width)
    bar = "█" * bar_n + "░" * (width - bar_n)

    return (
        f"[{prog.state}] {scope}  {done}/{total} ({pct:.0f}%)  "
        f"{human_duration(elapsed)} elapsed  {eta}\n"
        f"[{bar}]"
    )


def emit_json(payload) -> None:
    json.dump(payload, sys.stdout, indent=2, default=str, sort_keys=True)
    sys.stdout.write("\n")
    sys.stdout.flush()


def stderr(*parts) -> None:
    print(*parts, file=sys.stderr, flush=True)
