"""Run an indexing job with progress reporting and a fork-detached background mode.

The indexer itself (in indexer.py) is plain code that knows nothing about
processes; this module wraps it with:

- progress writes (so concurrent hook calls can read state)
- fork-detach so the hook returns to Claude Code immediately
- exception capture (errors land in the progress file as state=error so the
  next hook call can surface them on stderr instead of vanishing)
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import config as config_mod, indexer, paths, progress as progress_mod
from .embedder import resolve as resolve_embedder


def _run_inline(scope: Path, kind: str) -> None:
    """The actual work, run in whatever process we're in.

    `kind` is "indexing" (first build) or "refreshing" (incremental).
    """
    index_dir = scope / paths.INDEX_DIR_NAME
    index_dir.mkdir(parents=True, exist_ok=True)

    prog = progress_mod.Progress(
        state=kind,
        started_at=time.time(),
        files_done=0,
        files_total=0,
        pid=os.getpid(),
    )
    progress_mod.write(index_dir, prog)

    cfg = config_mod.load()

    try:
        emb = resolve_embedder(cfg.get("embedder", default={}) or {})

        opts = indexer.IndexOptions(
            target_chars=int(cfg.get("chunking", "target_chars", default=1500) or 1500),
            overlap_chars=int(cfg.get("chunking", "overlap_chars", default=200) or 200),
            max_file_size_mb=float(cfg.get("walker", "max_file_size_mb", default=1.0) or 1.0),
            respect_gitignore=bool(cfg.get("walker", "respect_gitignore", default=True)),
            full_rebuild=False,
        )

        # tee the indexer's progress messages into our progress file so
        # bare `rag` and any future log readers can see live counters.
        # Three message shapes the indexer produces:
        #   "walk: N candidate files"          -> files_total
        #   "embed: N files to (re)index"      -> files_total
        #   "progress: i/N files"              -> files_done, files_total
        def _on_progress(msg: str) -> None:
            cur = progress_mod.read(index_dir)
            cur.message = msg
            if msg.startswith("walk:") or msg.startswith("embed:"):
                parts = msg.split()
                try:
                    cur.files_total = int(parts[1])
                except (IndexError, ValueError):
                    pass
            elif msg.startswith("progress:"):
                # "progress: 1240/3815 files"
                parts = msg.split()
                if len(parts) >= 2 and "/" in parts[1]:
                    a, _, b = parts[1].partition("/")
                    try:
                        cur.files_done = int(a)
                        cur.files_total = int(b)
                    except ValueError:
                        pass
            progress_mod.write(index_dir, cur)

        stats = indexer.index_folder(scope, emb, opts, progress=_on_progress)
        progress_mod.mark_refresh(index_dir)

        last_run = progress_mod.LastRun(
            finished_at=time.time(),
            elapsed_seconds=max(0.0, time.time() - prog.started_at),
            kind=kind,
            files_total=int(stats.get("files_total") or 0),
            files_indexed=int(stats.get("files_indexed") or 0),
            files_pruned=int(stats.get("files_pruned") or 0),
            chunks_added=int(stats.get("chunks_added") or 0),
        )
        progress_mod.write_last_run(index_dir, last_run)

        _maybe_notify(scope, kind, last_run, cfg)

        # Successful run: clear the in-progress marker.
        progress_mod.clear(index_dir)
    except Exception as e:
        # Persist the error so the next hook call surfaces it on stderr,
        # rather than silently disappearing into the indexer.log.
        err = progress_mod.Progress(
            state="error",
            started_at=prog.started_at,
            files_done=0,
            files_total=0,
            pid=os.getpid(),
            message=f"{type(e).__name__}: {e}",
        )
        progress_mod.write(index_dir, err)
        traceback.print_exc()
        return


def fork_detach_index(scope: Path, kind: str = "indexing") -> int | None:
    """Fork-detach a child process to run an index/refresh job.

    Returns the child pid, or None if we are the child (so the caller knows
    not to come back). The parent returns immediately so Claude Code is
    not blocked.
    """
    try:
        pid = os.fork()
    except OSError as e:
        # Some sandboxes don't allow fork. Fall back to inline.
        print(f"hydra-rag-hooks: fork failed ({e}); indexing inline.", file=sys.stderr, flush=True)
        _run_inline(scope, kind)
        return os.getpid()

    if pid != 0:
        # Parent: write a placeholder progress entry so a near-immediate
        # follow-up `rag:` sees "indexing in progress" even before the
        # child has had time to start writing its own progress.
        index_dir = scope / paths.INDEX_DIR_NAME
        index_dir.mkdir(parents=True, exist_ok=True)
        placeholder = progress_mod.Progress(
            state=kind,
            started_at=time.time(),
            files_done=0,
            files_total=0,
            pid=pid,
        )
        progress_mod.write(index_dir, placeholder)
        return pid

    # Child: detach from the controlling tty / process group, redirect
    # stdio, then run the work.
    os.setsid()
    # Don't write to whatever stdio Claude Code gave us; redirect to the
    # cache log so accidental prints land somewhere debuggable.
    log = paths.cache_dir() / "indexer.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = open(log, "ab", buffering=0)
        os.dup2(f.fileno(), 0)
        os.dup2(f.fileno(), 1)
        os.dup2(f.fileno(), 2)
    except OSError:
        pass

    # Make sure SIGTERM during shutdown doesn't leave a half-written index.
    def _cleanup(_signum=None, _frame=None) -> None:
        index_dir = scope / paths.INDEX_DIR_NAME
        progress_mod.clear(index_dir)
        os._exit(143)

    signal.signal(signal.SIGTERM, _cleanup)

    _run_inline(scope, kind)
    os._exit(0)


def _maybe_notify(scope: Path, kind: str, run: progress_mod.LastRun, cfg) -> None:
    """Fire a desktop notification on initial-index completion.

    Refreshes are intentionally quiet (they happen every 5 min). Only the
    first-build "indexing" run gets a notification. Disable via
    `notifications.on_index_complete: false` in config.
    """
    if kind != "indexing":
        return
    if not bool(cfg.get("notifications", "on_index_complete", default=True)):
        return
    notify_send = shutil.which("notify-send")
    if not notify_send:
        return
    summary = "hydra-rag-hooks"
    body = (
        f"{scope.name} indexed: {run.chunks_added} chunks across "
        f"{run.files_indexed} files ({int(run.elapsed_seconds)}s)"
    )
    try:
        subprocess.Popen(
            [notify_send, "--app-name=hydra-rag-hooks", "-u", "low", summary, body],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        pass


def maybe_refresh(scope: Path) -> None:
    """If the existing index is past its refresh interval AND nothing else
    is currently working on it, fork-detach an incremental refresh.

    Cheap: if we are within the refresh interval, this is a no-op.
    """
    index_dir = scope / paths.INDEX_DIR_NAME
    if progress_mod.is_active(index_dir):
        return
    if not progress_mod.needs_refresh(index_dir):
        return
    fork_detach_index(scope, kind="refreshing")
