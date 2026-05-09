"""crh ls - list registered stores."""

from __future__ import annotations

from pathlib import Path

from .. import paths, progress as progress_mod, registry
from . import _common


def _row(entry) -> dict:
    scope = Path(entry.path).resolve()
    index_dir = scope / paths.INDEX_DIR_NAME
    last_run = progress_mod.read_last_run(index_dir)
    is_active = progress_mod.is_active(index_dir)
    populated = (index_dir / "chunks.lance").is_dir()

    if is_active:
        prog = progress_mod.read(index_dir)
        state = prog.state
    elif populated:
        state = "ready"
    elif index_dir.is_dir():
        state = "empty"
    else:
        state = "absent"

    return {
        "path": str(scope),
        "state": state,
        "tags": list(entry.tags),
        "embedder": entry.embedder,
        "dim": entry.dim,
        "chunks": (last_run.chunks_added if last_run else None),
        "files": (
            (last_run.files_indexed or last_run.files_total) if last_run else None
        ),
        "auto_refresh": (index_dir / ".auto-refresh").exists(),
    }


def run(args) -> int:
    entries = registry.load()
    rows = [_row(e) for e in entries]

    if args.json:
        _common.emit_json(rows)
        return 0

    if not rows:
        print("(no registered stores)")
        return 0

    # Determine column widths.
    path_w = max(len("PATH"), max(len(r["path"]) for r in rows))
    state_w = max(len("STATE"), max(len(r["state"]) for r in rows))

    print(f"{'PATH':<{path_w}}  {'STATE':<{state_w}}  CHUNKS    FILES  TAGS")
    for r in rows:
        chunks = str(r["chunks"]) if r["chunks"] is not None else "-"
        files = str(r["files"]) if r["files"] is not None else "-"
        tags = ",".join(r["tags"]) if r["tags"] else ""
        auto = " (auto)" if r["auto_refresh"] else ""
        print(
            f"{r['path']:<{path_w}}  {r['state']:<{state_w}}  "
            f"{chunks:>6}  {files:>5}  {tags}{auto}"
        )
    return 0
