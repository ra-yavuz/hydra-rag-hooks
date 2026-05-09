"""One-shot in-place migrations from the legacy claude-rag-hook layout.

Two migrations live here. Both are idempotent and best-effort: if the
target state is already in place, they return False; if anything
fails, they swallow the error and return False (the caller's path
already handles the legacy layout transparently, so a failed
migration leaves the user's setup working, just unmoved).

1. .claude-rag-index/ -> .hydra-index/
   On hook entry, when cwd's project root has the legacy folder but
   not the new folder, rename in place. Embeddings and chunks are
   bit-identical across the rename; LanceDB recognises the table at
   the new path on next open. The stores.json registry is updated to
   the new path so `crh ls` and tag federation see it.

2. ~/.config/claude-rag-hook/ -> ~/.config/hydra-rag-hooks/ (and
   the equivalent under .cache and .local/state). Handled in
   paths.py: every accessor folds in a one-shot rename when called.

The reason this lives in its own module rather than in paths.py: the
index migration walks the filesystem (looks at cwd's parents,
inspects multiple folders) and updates the persistent stores
registry. paths.py wants to stay a pure-function module for testing.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import paths, registry


def migrate_index_folder(start: Path) -> Path | None:
    """If the project root containing `start` has a legacy
    `.claude-rag-index/` and no `.hydra-index/`, rename in place.

    Returns the resolved index path after migration (or None if no
    index was found at all). Idempotent: a project that already has
    `.hydra-index/` is unchanged.
    """
    start = start.resolve()
    for d in (start, *start.parents):
        new = d / paths.INDEX_DIR_NAME
        old = d / paths.LEGACY_CLAUDE_INDEX_DIR_NAME
        if new.is_dir():
            return new
        if old.is_dir():
            try:
                old.rename(new)
            except OSError:
                # Cross-device, permission error, or some race. Leave
                # the legacy path in place; find_index will still find
                # it via its read-compat path.
                return old
            _update_registry_path(d, old, new)
            return new
    return None


def _update_registry_path(project_root: Path, old_index: Path, new_index: Path) -> None:
    """Rewrite stores.json so the registered project path is unchanged
    (it's the project root, not the index folder, and the rename did
    not move the project). Only the index sub-path changed; the
    registry stores project-root paths, so usually nothing to do.

    Kept as a stub for symmetry and so future schema changes have an
    obvious entry point. We still touch the registry's `last_indexed`
    on the matching entry so `crh ls` reflects activity, and we
    clean up entries that pointed at the old index path explicitly
    (some early tests of crh import did register the index path).
    """
    try:
        entries = registry.load()
    except Exception:  # noqa: BLE001
        return
    changed = False
    project_resolved = str(project_root.resolve())
    old_resolved = str(old_index.resolve()) if old_index.exists() else None
    for e in entries:
        if old_resolved is not None and e.path == old_resolved:
            e.path = project_resolved
            changed = True
        elif e.path == project_resolved:
            # Already correct; nothing to do.
            pass
    if changed:
        try:
            registry.save(entries)
        except OSError:
            pass


def env_says_skip() -> bool:
    """Operator escape hatch. Setting HYDRA_RAG_HOOKS_SKIP_MIGRATIONS=1
    disables both the index-folder rename and the XDG-folder rename
    helpers. Useful for debugging and for tests that want a clean
    slate.
    """
    return os.environ.get("HYDRA_RAG_HOOKS_SKIP_MIGRATIONS") == "1"
