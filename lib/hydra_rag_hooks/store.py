"""LanceDB store wrapper.

One LanceDB table per index, named `chunks`. Schema:

    rel: str         relative path of source file
    start_line: int
    end_line: int
    kind: str        'code' or 'prose'
    text: str        chunk text
    vector: list[float]  embedding

A `meta.yaml` at the index root records which embedder produced the
vectors so a later query knows what to embed the user's text with. A
`files.json` records (rel, mtime, size) so incremental refresh can skip
unchanged files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

META_NAME = "meta.yaml"
FILES_NAME = "files.json"
TABLE_NAME = "chunks"


@dataclass
class Hit:
    rel: str
    start_line: int
    end_line: int
    kind: str
    text: str
    score: float


def _import_lancedb():
    try:
        import lancedb  # type: ignore
        import pyarrow as pa  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "lancedb and pyarrow are required. Install with `pip install lancedb pyarrow`."
        ) from e
    return lancedb, pa


def write_meta(index_dir: Path, embedder_kind: str, embedder_model: str, dim: int) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "embedder": {
            "kind": embedder_kind,
            "model": embedder_model,
            "dim": dim,
        },
        "schema_version": 1,
    }
    with (index_dir / META_NAME).open("w", encoding="utf-8") as f:
        yaml.safe_dump(meta, f, sort_keys=False)


def read_meta(index_dir: Path) -> dict[str, Any]:
    p = index_dir / META_NAME
    # hydra-llm uses the same filename, so reading transparently works.
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def read_files_manifest(index_dir: Path) -> dict[str, dict[str, Any]]:
    p = index_dir / FILES_NAME
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f) or {}
    return data if isinstance(data, dict) else {}


def write_files_manifest(index_dir: Path, manifest: dict[str, dict[str, Any]]) -> None:
    """Atomic manifest write.

    The manifest is the resumability anchor: on the next indexer run,
    files whose (size, mtime) match this manifest are skipped. A
    half-written or empty manifest (eg. crash mid-write) means the
    next run re-embeds everything, doubling chunks until pruned.
    Write to a temp file in the same directory, fsync, then rename
    onto the real path so any reader sees either the old or the new
    manifest, never a torn one.
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    final = index_dir / FILES_NAME
    tmp = final.with_suffix(final.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        try:
            import os as _os
            _os.fsync(f.fileno())
        except OSError:
            pass
    tmp.replace(final)


def open_db(index_dir: Path):
    lancedb, _pa = _import_lancedb()
    index_dir.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(index_dir))


def open_table(index_dir: Path, dim: int):
    lancedb, pa = _import_lancedb()
    db = open_db(index_dir)
    if TABLE_NAME in db.table_names():
        return db.open_table(TABLE_NAME)
    schema = pa.schema([
        pa.field("rel", pa.string()),
        pa.field("start_line", pa.int32()),
        pa.field("end_line", pa.int32()),
        pa.field("kind", pa.string()),
        pa.field("text", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), dim)),
    ])
    return db.create_table(TABLE_NAME, schema=schema)


def add_rows(table, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    table.add(rows)


def delete_rel(table, rel: str) -> None:
    """Drop every row whose `rel` matches. Used to refresh changed files."""
    safe = rel.replace("'", "''")
    table.delete(f"rel = '{safe}'")


def search(table, vector: list[float], top_k: int) -> list[Hit]:
    results = table.search(vector).limit(top_k).to_list()
    hits: list[Hit] = []
    for r in results:
        # lancedb returns either `_distance` or `_score`. Lower distance is better.
        dist = r.get("_distance")
        score = -float(dist) if dist is not None else float(r.get("_score") or 0.0)
        hits.append(
            Hit(
                rel=str(r.get("rel") or ""),
                start_line=int(r.get("start_line") or 0),
                end_line=int(r.get("end_line") or 0),
                kind=str(r.get("kind") or ""),
                text=str(r.get("text") or ""),
                score=score,
            )
        )
    return hits


def rrf_fuse(lists: list[list[Hit]], k: int = 60, top_k: int = 5) -> list[Hit]:
    """Reciprocal Rank Fusion across multiple ranked hit lists.

    Each (rel, start_line, end_line) is a fusion key. RRF score is
    sum(1 / (k + rank)) across lists where the doc appears.
    """
    by_key: dict[tuple[str, int, int], tuple[Hit, float]] = {}
    for hits in lists:
        for rank, h in enumerate(hits, start=1):
            key = (h.rel, h.start_line, h.end_line)
            inc = 1.0 / (k + rank)
            if key in by_key:
                cur_h, cur_s = by_key[key]
                by_key[key] = (cur_h, cur_s + inc)
            else:
                by_key[key] = (h, inc)
    fused = sorted(by_key.values(), key=lambda x: -x[1])[:top_k]
    out: list[Hit] = []
    for h, s in fused:
        out.append(Hit(rel=h.rel, start_line=h.start_line, end_line=h.end_line,
                       kind=h.kind, text=h.text, score=s))
    return out
