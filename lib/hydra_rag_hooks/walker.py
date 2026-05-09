"""Walk a folder, respecting .gitignore and the builtin skip list."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import pathspec

from . import classifier


@dataclass
class WalkOptions:
    max_file_size_mb: float = 1.0
    respect_gitignore: bool = True
    extra_excludes: list[str] | None = None
    extra_includes: list[str] | None = None


@dataclass
class WalkedFile:
    path: Path
    rel: str
    kind: str
    size: int
    mtime: float


def _load_gitignore(root: Path) -> pathspec.PathSpec | None:
    gi = root / ".gitignore"
    if not gi.exists():
        return None
    try:
        with gi.open("r", encoding="utf-8", errors="ignore") as f:
            return pathspec.PathSpec.from_lines("gitwildmatch", f)
    except OSError:
        return None


def walk(root: Path, opts: WalkOptions) -> Iterator[WalkedFile]:
    root = root.resolve()
    max_bytes = int(opts.max_file_size_mb * 1024 * 1024)
    gi = _load_gitignore(root) if opts.respect_gitignore else None
    excludes = pathspec.PathSpec.from_lines("gitwildmatch", opts.extra_excludes or [])
    includes = pathspec.PathSpec.from_lines("gitwildmatch", opts.extra_includes or [])

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        # Prune skip-dirs in place so os.walk doesn't descend into them.
        dirnames[:] = [
            n for n in dirnames
            if n not in classifier.SKIP_DIRS and not n.startswith(".git")
        ]
        # Apply gitignore to dirs (a hit means skip the whole subtree).
        if gi is not None:
            kept = []
            for n in dirnames:
                rel_dir = (d / n).relative_to(root).as_posix() + "/"
                if not gi.match_file(rel_dir):
                    kept.append(n)
            dirnames[:] = kept

        for name in filenames:
            p = d / name
            try:
                st = p.stat()
            except OSError:
                continue
            if not _is_regular(st.st_mode):
                continue
            rel = p.relative_to(root).as_posix()
            forced_in = bool(opts.extra_includes) and includes.match_file(rel)
            if not forced_in:
                if gi is not None and gi.match_file(rel):
                    continue
                if excludes.match_file(rel):
                    continue
            if st.st_size > max_bytes:
                continue
            kind = classifier.classify(p)
            if kind is None:
                continue
            yield WalkedFile(path=p, rel=rel, kind=kind, size=st.st_size, mtime=st.st_mtime)


def _is_regular(mode: int) -> bool:
    import stat
    return stat.S_ISREG(mode)


def all_files(root: Path, opts: WalkOptions) -> list[WalkedFile]:
    return list(walk(root, opts))


def filter_by_kind(files: Iterable[WalkedFile], kind: str) -> list[WalkedFile]:
    return [f for f in files if f.kind == kind]
