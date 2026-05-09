"""crh status - see what's going on with the index.

One-shot mode reads .progress + .last_run.json and prints a
human-readable summary (or JSON). Watch mode redraws every second
until indexing finishes or the user Ctrl-Cs out (the indexer keeps
running; the watcher just stops watching).

This is the subcommand that fixes today's specific pain: a way to
know what a long-running detached indexer is up to without going
through Claude.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .. import config as config_mod, paths, progress as progress_mod, registry, store
from . import _common


def _embedder_hint(index_dir: Path) -> dict:
    """Compare the index's recorded embedder against the user's
    currently configured one. Returns a dict with `recorded` (the
    model the index was built with) and `configured` (what the user
    would build with today). If they differ, the caller can surface
    a hint that the user might want `crh refresh --rebuild`.

    Pure filesystem reads; never loads an embedder.
    """
    out = {"recorded_kind": None, "recorded_model": None,
           "recorded_dim": None, "configured_kind": None,
           "configured_model": None, "mismatch": False}
    try:
        meta = store.read_meta(index_dir)
        e = meta.get("embedder") or {}
        if isinstance(e, dict):
            out["recorded_kind"] = e.get("kind")
            out["recorded_model"] = e.get("model")
            out["recorded_dim"] = e.get("dim")
    except Exception:  # noqa: BLE001
        pass
    try:
        cfg = config_mod.load()
        cfg_emb = cfg.get("embedder", default={}) or {}
        if isinstance(cfg_emb, dict):
            out["configured_kind"] = cfg_emb.get("kind")
            out["configured_model"] = cfg_emb.get("model")
    except Exception:  # noqa: BLE001
        pass
    rk, rm = out["recorded_kind"], out["recorded_model"]
    ck, cm = out["configured_kind"], out["configured_model"]
    if rk and rm and ck and cm and (rk != ck or rm != cm):
        out["mismatch"] = True
    return out


def _read_state(scope: Path) -> dict:
    """Filesystem-only snapshot. No embedder, no LanceDB. Cheap."""
    index_dir = _common.index_dir_for(scope)
    prog = progress_mod.read(index_dir)
    last_run = progress_mod.read_last_run(index_dir)
    is_active = progress_mod.is_active(index_dir)
    populated = (index_dir / "chunks.lance").is_dir()
    emb_hint = _embedder_hint(index_dir) if populated else {}

    if is_active:
        state = prog.state  # "indexing" or "refreshing"
    elif prog.state == "error":
        state = "error"
    elif prog.state in ("indexing", "refreshing"):
        # .progress claims an active job but the pid is dead. The
        # previous run was killed/crashed mid-flight. The on-disk
        # data is partial; user should resume with `crh refresh` or
        # blow it away.
        state = "interrupted"
    elif populated:
        state = "ready"
    elif index_dir.is_dir():
        state = "empty"
    else:
        state = "absent"

    return {
        "scope": str(scope),
        "state": state,
        "files_done": prog.files_done,
        "files_total": prog.files_total,
        "elapsed_seconds": (
            max(0.0, time.time() - prog.started_at)
            if is_active and prog.started_at > 0
            else 0.0
        ),
        "started_at": prog.started_at if is_active else 0.0,
        "message": prog.message,
        "error_message": prog.message if prog.state == "error" else None,
        "last_run": (
            {
                "kind": last_run.kind,
                "finished_at": last_run.finished_at,
                "ago_seconds": max(0.0, time.time() - last_run.finished_at),
                "elapsed_seconds": last_run.elapsed_seconds,
                "files_total": last_run.files_total,
                "files_indexed": last_run.files_indexed,
                "files_pruned": last_run.files_pruned,
                "chunks_added": last_run.chunks_added,
            }
            if last_run is not None
            else None
        ),
        "log_path": str(paths.cache_dir() / "indexer.log"),
        "auto_refresh": (index_dir / ".auto-refresh").exists(),
        "embedder": emb_hint,
    }


def _format_human(snap: dict) -> str:
    state = snap["state"]
    scope = snap["scope"]
    auto = " (auto-refresh on)" if snap["auto_refresh"] else ""

    if state == "absent":
        return f"[absent] {scope}{auto}\n        no index yet. `crh index` to build."
    if state == "empty":
        return f"[empty] {scope}{auto}\n        index folder exists but is empty. `crh index` to (re)build."
    if state == "interrupted":
        done = snap["files_done"]
        total = snap["files_total"] or 0
        msg = snap.get("message") or "no message"
        return (
            f"[interrupted] {scope}{auto}\n"
            f"        previous run was killed/crashed mid-flight ({done}/{total} files)\n"
            f"        last message: {msg}\n"
            f"        recover: `crh refresh` to resume from manifest, or "
            f"`crh forget` to drop the partial index."
        )
    if state == "error":
        msg = snap.get("error_message") or "unknown error"
        return (
            f"[error] {scope}{auto}\n"
            f"        last attempt failed: {msg}\n"
            f"        log: {snap['log_path']}\n"
            f"        recover: pip install --user fastembed lancedb pyarrow, "
            f"then rm {snap['scope']}/.claude-rag-index/.progress and `crh index`."
        )
    if state in ("indexing", "refreshing"):
        done = snap["files_done"]
        total = snap["files_total"] or 0
        elapsed = _common.human_duration(snap["elapsed_seconds"])
        if total > 0:
            pct = done / total * 100
            counter = f"{done}/{total} ({pct:.0f}%)"
        else:
            counter = f"{done} files so far"
        return (
            f"[{state}] {scope}{auto}\n"
            f"        progress: {counter}\n"
            f"        elapsed: {elapsed}\n"
            f"        log: {snap['log_path']}"
        )
    if state == "ready":
        last = snap["last_run"]
        emb = snap.get("embedder") or {}
        mismatch_line = ""
        if emb.get("mismatch"):
            mismatch_line = (
                f"\n        embedder: built with {emb.get('recorded_kind')}:"
                f"{emb.get('recorded_model')}, but config now uses "
                f"{emb.get('configured_kind')}:{emb.get('configured_model')}.\n"
                f"        retrieval still works against this index. "
                f"To migrate to the configured embedder, run "
                f"`crh refresh --rebuild`."
            )
        if last is not None:
            ago = _common.human_duration(last["ago_seconds"])
            took = _common.human_duration(last["elapsed_seconds"])
            files = last["files_indexed"] or last["files_total"]
            return (
                f"[ready] {scope}{auto}\n"
                f"        {last['chunks_added']} chunks across {files} files\n"
                f"        last {last['kind']}: {ago} ago (took {took})"
                f"{mismatch_line}"
            )
        return (
            f"[ready] {scope}{auto}\n"
            f"        index present (no run stats; built by an older "
            f"version or another tool)"
            f"{mismatch_line}"
        )
    return f"[{state}] {scope}{auto}"


def _render_one(scope: Path, want_json: bool) -> int:
    snap = _read_state(scope)
    if want_json:
        _common.emit_json(snap)
    else:
        print(_format_human(snap))
    return 0


def _render_all(want_json: bool) -> int:
    entries = registry.load()
    snaps = []
    for e in entries:
        scope = Path(e.path).resolve()
        snap = _read_state(scope)
        snap["tags"] = list(e.tags)
        snap["embedder"] = e.embedder
        snaps.append(snap)
    if want_json:
        _common.emit_json(snaps)
        return 0
    if not snaps:
        print("(no registered stores)")
        return 0
    for snap in snaps:
        print(_format_human(snap))
        if snap.get("tags"):
            print(f"        tags: {', '.join(snap['tags'])}")
        print()
    return 0


def _watch(scope: Path) -> int:
    """Live-redrawing display. Exits when state goes from
    indexing/refreshing to populated/error, or on Ctrl-C."""
    started_in_active = False
    # Hide cursor for a calmer redraw. Restore on exit.
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    try:
        while True:
            snap = _read_state(scope)
            state = snap["state"]
            if state in ("indexing", "refreshing"):
                started_in_active = True
                # Clear screen + render full progress block.
                sys.stdout.write("\033[2J\033[H")  # clear, home
                sys.stdout.write(_format_human(snap))
                sys.stdout.write(
                    "\n\n(Ctrl-C to stop watching; the indexer keeps running)\n"
                )
                sys.stdout.flush()
                time.sleep(1.0)
                continue

            # No longer active. If we never saw an active state,
            # this is just `crh status --watch` against an idle scope;
            # show the snapshot once and exit.
            sys.stdout.write("\033[2J\033[H")
            print(_format_human(snap))
            if started_in_active:
                if state == "ready":
                    print("\nFinished.")
                elif state == "error":
                    print("\nIndexer failed; see the log path above.")
                else:
                    print(f"\nIndexer state: {state}")
            return 0
    finally:
        # Restore cursor.
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def run(args) -> int:
    if args.all:
        if args.watch:
            _common.stderr("--watch and --all are mutually exclusive")
            return 2
        return _render_all(args.json)
    scope = _common.find_scope(_common.resolve_path(args.path))
    if scope is None:
        _common.stderr(
            f"crh status: no index or project marker found at or above {_common.resolve_path(args.path)}"
        )
        return 1
    if args.watch:
        if args.json:
            _common.stderr("--watch and --json are mutually exclusive")
            return 2
        return _watch(scope)
    return _render_one(scope, args.json)
