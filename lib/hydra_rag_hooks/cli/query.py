"""crh query - one-shot retrieval to stdout.

Useful for piping to grep, scripts, or comparing what RAG would
surface against what you got from Claude. Same chunk format the
hook injects into the prompt: `--- rel:start-end (kind) ---`
header followed by the verbatim text block.
"""

from __future__ import annotations

from pathlib import Path

from .. import config as config_mod, paths, retrieval
from . import _common


def run(args) -> int:
    cfg = config_mod.load()
    scope = _common.resolve_path(args.scope) if args.scope else _common.find_scope(Path.cwd())
    if scope is None:
        _common.stderr(
            "crh query: no index found in or above cwd. "
            "Pass --scope <path>, or `crh index` first."
        )
        return 1

    index_dir = paths.find_index(scope)
    if index_dir is None:
        _common.stderr(f"crh query: no index at or above {scope}")
        return 1

    try:
        hits = retrieval.retrieve(args.text, [index_dir], top_k=args.top_k, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        _common.stderr(f"crh query: retrieval error: {e}")
        return 1

    if args.json:
        _common.emit_json([
            {
                "rel": h.rel,
                "start_line": h.start_line,
                "end_line": h.end_line,
                "kind": h.kind,
                "text": h.text,
            }
            for h in hits
        ])
        return 0

    if not hits:
        print("(no relevant chunks found)")
        return 0

    for h in hits:
        if h.start_line and h.end_line:
            print(f"--- {h.rel}:{h.start_line}-{h.end_line} ({h.kind}) ---")
        else:
            print(f"--- {h.rel} ({h.kind}) ---")
        print(h.text)
        print()
    return 0
