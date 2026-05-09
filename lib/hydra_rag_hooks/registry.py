"""Per-user registry of indexed folders.

Stored at $XDG_STATE_HOME/hydra-rag-hooks/stores.json. Same shape as
hydra-llm's stores.json: a flat list of {path, tags, embedder, dim,
created, last_indexed}.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths


@dataclass
class StoreEntry:
    path: str
    tags: list[str] = field(default_factory=list)
    embedder: str = ""
    dim: int = 0
    created: str = ""
    last_indexed: str = ""

    def with_now(self) -> "StoreEntry":
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        if not self.created:
            self.created = now
        self.last_indexed = now
        return self


def _path() -> Path:
    return paths.stores_registry()


def load() -> list[StoreEntry]:
    p = _path()
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f) or []
    return [StoreEntry(**e) for e in raw if isinstance(e, dict)]


def save(entries: list[StoreEntry]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump([asdict(e) for e in entries], f, indent=2)
        f.write("\n")


def upsert(entry: StoreEntry) -> None:
    entries = load()
    target = str(Path(entry.path).resolve())
    entry.path = target
    for i, e in enumerate(entries):
        if str(Path(e.path).resolve()) == target:
            entry.created = e.created or entry.created
            # Merge tags (set union, preserve order).
            tags = list(dict.fromkeys([*e.tags, *entry.tags]))
            entry.tags = tags
            entries[i] = entry.with_now()
            save(entries)
            return
    entries.append(entry.with_now())
    save(entries)


def remove(path: Path) -> bool:
    entries = load()
    target = str(path.resolve())
    new_entries = [e for e in entries if str(Path(e.path).resolve()) != target]
    if len(new_entries) == len(entries):
        return False
    save(new_entries)
    return True


def by_tag(tag: str) -> list[StoreEntry]:
    return [e for e in load() if tag in e.tags]


def all_paths() -> list[Path]:
    return [Path(e.path) for e in load()]
