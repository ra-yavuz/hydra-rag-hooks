"""Query-time retrieval: resolve indexes, embed query, search, format."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths, registry, store, daemon as daemon_mod, config as config_mod
from .embedder import resolve as resolve_embedder, Embedder
from .store import Hit


@dataclass
class RetrievalResult:
    hits: list[Hit]
    indexes: list[Path]


def resolve_indexes(cwd: Path, tag: str | None) -> list[Path]:
    """Return the list of index directories to query.

    - tag is None: walk up from cwd, the nearest index wins.
    - tag is "all": every registered store.
    - tag is a string: every registered store carrying that tag.
    """
    if tag is None:
        idx = paths.find_index(cwd)
        return [idx] if idx else []
    if tag == "all":
        out = []
        for p in registry.all_paths():
            for name in (paths.INDEX_DIR_NAME, paths.HYDRA_INDEX_DIR_NAME):
                cand = p / name
                if cand.is_dir():
                    out.append(cand)
                    break
        return out
    out = []
    for entry in registry.by_tag(tag):
        p = Path(entry.path)
        for name in (paths.INDEX_DIR_NAME, paths.HYDRA_INDEX_DIR_NAME):
            cand = p / name
            if cand.is_dir():
                out.append(cand)
                break
    return out


def _embedder_for_index(index_dir: Path, cfg_data: dict[str, Any]) -> Embedder:
    """Build an embedder that matches what the index was built with.

    Falls back to the user's configured embedder if meta.yaml is silent.
    """
    meta = store.read_meta(index_dir)
    e = (meta.get("embedder") or {}) if isinstance(meta.get("embedder"), dict) else {}
    chosen = dict(cfg_data.get("embedder") or {})
    if e:
        chosen["kind"] = e.get("kind") or chosen.get("kind")
        chosen["model"] = e.get("model") or chosen.get("model")
    return resolve_embedder(chosen)


def _embed_query_via_daemon_or_inline(text: str, embedder: Embedder, daemon_enabled: bool) -> list[float]:
    """Use the warm daemon if it is alive (or can be started), else embed inline.

    Daemon-startup failures (no spawn permission, wrong path, missing
    deps, 5s startup timeout, unwritable cache dir) must NOT make the
    whole retrieval go silent: we just fall back to inline embedding.
    The daemon is a latency optimisation, not a correctness path.
    """
    if not daemon_enabled:
        return embedder.embed_query(text)
    try:
        if not daemon_mod.is_alive():
            try:
                daemon_mod.spawn(detach=True)
            except (OSError, RuntimeError):
                return embedder.embed_query(text)
        resp = daemon_mod.call("embed_query", {"text": text}, timeout=30.0)
        if resp.get("ok"):
            v = resp.get("vector") or []
            if v:
                return [float(x) for x in v]
        return embedder.embed_query(text)
    except (OSError, RuntimeError):
        return embedder.embed_query(text)


def retrieve(query: str, indexes: list[Path], top_k: int = 5, cfg=None) -> list[Hit]:
    if not indexes:
        return []
    cfg = cfg or config_mod.load()
    cfg_data = cfg.data if hasattr(cfg, "data") else {}
    daemon_enabled = bool(cfg.get("daemon", "enabled", default=True)) if hasattr(cfg, "get") else True

    per_index_hits: list[list[Hit]] = []
    for idx in indexes:
        emb = _embedder_for_index(idx, cfg_data)
        try:
            qv = _embed_query_via_daemon_or_inline(query, emb, daemon_enabled)
        except Exception:
            continue
        try:
            table = store.open_table(idx, emb.dim)
            hits = store.search(table, qv, top_k=top_k)
            per_index_hits.append(hits)
        except Exception:
            continue

    if not per_index_hits:
        return []
    if len(per_index_hits) == 1:
        return per_index_hits[0][:top_k]
    return store.rrf_fuse(per_index_hits, top_k=top_k)


def format_context(hits: list[Hit], header: str = "<context>", footer: str = "</context>",
                   show_source_lines: bool = True) -> str:
    if not hits:
        return ""
    lines = [header]
    lines.append(
        "Retrieved by hydra-rag-hooks from your local index. "
        "Each block is verbatim text from a file in the indexed folder. "
        "Use it as ground truth for the user's question. "
        "If a block is irrelevant, ignore it."
    )
    for h in hits:
        if show_source_lines and h.start_line and h.end_line:
            lines.append(f"\n--- {h.rel}:{h.start_line}-{h.end_line} ({h.kind}) ---")
        else:
            lines.append(f"\n--- {h.rel} ({h.kind}) ---")
        lines.append(h.text)
    lines.append(footer)
    return "\n".join(lines)
