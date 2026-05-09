"""crh index / crh refresh - blocking with live progress.

Unlike the hook (which fork-detaches the indexer so Claude is not
held), the CLI runs inline. The user explicitly asked for this on
their shell; they want to wait, watch, and Ctrl-C cleanly.

Both commands share `_run_inline`. The only difference is whether
auto_index gates apply (initial index respects them; refresh
operates on an existing index and doesn't need them).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .. import (
    auto_index,
    config as config_mod,
    indexer,
    paths,
    progress as progress_mod,
)
from ..embedder import resolve as resolve_embedder
from . import _common


def _print_redraw(line: str) -> None:
    """Single-line redraw using \\r and ANSI clear-to-EOL.

    Cleaner than `\033[2J\033[H` for in-place progress bars while
    other output (warnings) can still scroll above.
    """
    sys.stdout.write("\r\033[K" + line)
    sys.stdout.flush()


def _run_inline(scope: Path, kind: str, watch: bool, want_json: bool,
                rebuild: bool = False) -> int:
    """Drive an indexing run synchronously in this process.

    `kind` is "indexing" (initial) or "refreshing" (incremental).
    `rebuild`: when True, drop the existing chunks.lance and manifest
    so the run re-embeds every file from scratch. Used for migrating
    between embedders (the new embedder's vectors won't match the
    old embedder's, so partial-update semantics don't apply).
    """
    index_dir = scope / paths.INDEX_DIR_NAME
    if progress_mod.is_active(index_dir):
        _common.stderr(
            f"crh: an index job for {scope} is already running "
            f"(pid {progress_mod.read(index_dir).pid}). "
            f"`crh status --watch` to monitor it, or kill that pid first."
        )
        return 1

    index_dir.mkdir(parents=True, exist_ok=True)

    # Persist a Progress entry so concurrent `crh status` calls see
    # this run as active (same shape the runner.py fork-detach path
    # uses).
    started_at = time.time()
    progress_mod.write(
        index_dir,
        progress_mod.Progress(
            state=kind,
            started_at=started_at,
            files_done=0,
            files_total=0,
            pid=__import__("os").getpid(),
        ),
    )

    cfg = config_mod.load()
    try:
        emb = resolve_embedder(cfg.get("embedder", default={}) or {})
    except Exception as e:
        progress_mod.write(
            index_dir,
            progress_mod.Progress(
                state="error",
                started_at=started_at,
                pid=__import__("os").getpid(),
                message=f"{type(e).__name__}: {e}",
            ),
        )
        _common.stderr(f"crh {kind}: failed to load embedder: {e}")
        return 1

    opts = indexer.IndexOptions(
        target_chars=int(cfg.get("chunking", "target_chars", default=1500) or 1500),
        overlap_chars=int(cfg.get("chunking", "overlap_chars", default=200) or 200),
        max_file_size_mb=float(cfg.get("walker", "max_file_size_mb", default=1.0) or 1.0),
        respect_gitignore=bool(cfg.get("walker", "respect_gitignore", default=True)),
        full_rebuild=rebuild,
    )

    last_redraw = 0.0

    def _on_progress(msg: str) -> None:
        nonlocal last_redraw
        cur = progress_mod.read(index_dir)
        cur.message = msg
        if msg.startswith("walk:") or msg.startswith("embed:"):
            parts = msg.split()
            try:
                cur.files_total = int(parts[1])
            except (IndexError, ValueError):
                pass
        elif msg.startswith("progress:"):
            parts = msg.split()
            if len(parts) >= 2 and "/" in parts[1]:
                a, _, b = parts[1].partition("/")
                try:
                    cur.files_done = int(a)
                    cur.files_total = int(b)
                except ValueError:
                    pass
        progress_mod.write(index_dir, cur)

        if watch and not want_json:
            now = time.monotonic()
            # Throttle redraw to 4 Hz so the terminal doesn't flicker.
            if now - last_redraw >= 0.25:
                _print_redraw(_format_compact(scope, cur))
                last_redraw = now

    try:
        stats = indexer.index_folder(scope, emb, opts, progress=_on_progress)
    except KeyboardInterrupt:
        if watch and not want_json:
            sys.stdout.write("\n")
        progress_mod.write(
            index_dir,
            progress_mod.Progress(
                state="error",
                started_at=started_at,
                pid=__import__("os").getpid(),
                message="interrupted by user (KeyboardInterrupt)",
            ),
        )
        _common.stderr(
            "crh: interrupted. Partial index left on disk; resume with "
            "`crh refresh` (size+mtime manifest skips already-completed files)."
        )
        return 130
    except Exception as e:  # noqa: BLE001
        if watch and not want_json:
            sys.stdout.write("\n")
        progress_mod.write(
            index_dir,
            progress_mod.Progress(
                state="error",
                started_at=started_at,
                pid=__import__("os").getpid(),
                message=f"{type(e).__name__}: {e}",
            ),
        )
        _common.stderr(f"crh {kind}: {type(e).__name__}: {e}")
        return 1

    elapsed = time.time() - started_at
    progress_mod.mark_refresh(index_dir)
    last_run = progress_mod.LastRun(
        finished_at=time.time(),
        elapsed_seconds=elapsed,
        kind=kind,
        files_total=int(stats.get("files_total") or 0),
        files_indexed=int(stats.get("files_indexed") or 0),
        files_pruned=int(stats.get("files_pruned") or 0),
        chunks_added=int(stats.get("chunks_added") or 0),
    )
    progress_mod.write_last_run(index_dir, last_run)
    progress_mod.clear(index_dir)

    if watch and not want_json:
        sys.stdout.write("\n")
        sys.stdout.flush()

    if want_json:
        _common.emit_json({
            "scope": str(scope),
            "kind": kind,
            "elapsed_seconds": elapsed,
            "stats": stats,
        })
    else:
        files = last_run.files_indexed or last_run.files_total
        print(
            f"done: {last_run.chunks_added} chunks across {files} files "
            f"in {_common.human_duration(elapsed)}"
        )
    return 0


def _format_compact(scope: Path, prog: progress_mod.Progress) -> str:
    """One-liner suitable for \\r redraw."""
    elapsed = max(0.0, time.time() - prog.started_at)
    total = max(0, prog.files_total or 0)
    done = max(0, prog.files_done or 0)
    if total > 0:
        pct = done / total * 100
        if done > 0 and elapsed > 0:
            rate = done / elapsed
            remaining = (total - done) / rate if rate > 0 else 0
            tail = f"~{_common.human_duration(remaining)} left"
        else:
            tail = "ETA pending"
        return (
            f"[{prog.state}] {done}/{total} ({pct:.0f}%)  "
            f"{_common.human_duration(elapsed)} elapsed  {tail}"
        )
    return f"[{prog.state}] {prog.message or 'starting...'}"


def run_index(args) -> int:
    target = _common.resolve_path(args.path)
    decision = auto_index.decide(target)
    if not decision.allow:
        _common.stderr(f"crh index: {decision.reason}")
        return 2
    scope = decision.scope
    assert scope is not None
    return _run_inline(
        scope, kind="indexing",
        watch=not args.no_watch, want_json=args.json,
        rebuild=getattr(args, "rebuild", False),
    )


def run_refresh(args) -> int:
    target = _common.resolve_path(args.path)
    existing = paths.find_index(target)
    if existing is None:
        _common.stderr(
            f"crh refresh: no index found at or above {target}. "
            f"`crh index` to build one first."
        )
        return 2
    scope = existing.parent
    return _run_inline(
        scope, kind="refreshing",
        watch=not args.no_watch, want_json=args.json,
        rebuild=getattr(args, "rebuild", False),
    )
