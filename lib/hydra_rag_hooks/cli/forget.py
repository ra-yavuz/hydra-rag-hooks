"""crh forget - delete the index for a folder.

Asks for confirmation by default (the index can take hours to
rebuild). --yes skips the prompt for scripting.
"""

from __future__ import annotations

import shutil
import sys

from .. import paths, progress as progress_mod, registry
from . import _common


def run(args) -> int:
    target = _common.resolve_path(args.path)
    index_dir = paths.find_index(target)
    if index_dir is None:
        _common.stderr(f"crh forget: no index at or above {target}")
        return 1

    scope = index_dir.parent

    # Refuse to forget while a job is running, to avoid leaving the
    # indexer in a "what happened to my files" state.
    if progress_mod.is_active(index_dir):
        prog = progress_mod.read(index_dir)
        _common.stderr(
            f"crh forget: refusing; an indexing job is running "
            f"(pid {prog.pid}). Stop it first: kill {prog.pid}, "
            f"then re-run."
        )
        return 1

    if not args.yes:
        sys.stdout.write(
            f"This will permanently delete:\n"
            f"  {index_dir}\n"
            f"and remove the registry entry for:\n"
            f"  {scope}\n"
            f"Continue? [y/N] "
        )
        sys.stdout.flush()
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("aborted")
            return 0

    shutil.rmtree(index_dir, ignore_errors=True)
    registry.remove(scope)
    print(f"forgot {scope}")
    return 0
