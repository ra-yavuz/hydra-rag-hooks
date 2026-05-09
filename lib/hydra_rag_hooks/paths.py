"""Filesystem paths used by hydra-rag-hooks.

XDG-compliant locations for config, cache, and per-user state.

Index folder naming. The unified project name is `.hydra-index/` (also
what hydra-llm uses). The legacy folder name `.claude-rag-index/` is
read transparently for back-compat with claude-rag-hook v0.6 and
older. New indexes are written to `.hydra-index/`. The hook performs
a one-shot rename on first invocation: when a project root has
`.claude-rag-index/` but no `.hydra-index/`, the directory is
renamed in place; existing data and chunks are preserved bit-perfect
and stores.json is updated atomically. See lib/hydra_rag_hooks/
migrate.py for the rename code.
"""

from __future__ import annotations

import os
from pathlib import Path


# Primary index folder. New indexes use this name; existing
# .claude-rag-index/ folders are read transparently and renamed in
# place on first hook run.
INDEX_DIR_NAME = ".hydra-index"

# Legacy folder name (claude-rag-hook v0.6 and earlier). Read-compat
# only: never written, always migrated to INDEX_DIR_NAME on first
# touch.
LEGACY_CLAUDE_INDEX_DIR_NAME = ".claude-rag-index"

# Kept for source-compatibility with the imports that already use this
# name (the resolver below treats it as another alias for the primary
# folder, which it is).
HYDRA_INDEX_DIR_NAME = INDEX_DIR_NAME


def _xdg(env: str, fallback: Path) -> Path:
    val = os.environ.get(env)
    return Path(val) if val else fallback


# Unified hydra-* family layout. Both this package and hydra-llm live
# under a single top-level XDG namespace ("hydra-llm") so the two tools
# share the same on-disk family and can later coordinate (model cache,
# embedder catalog, federated retrieval) without each owning its own
# silo. Within that namespace each tool keeps its own subdirectory for
# files unique to it: `rag-hooks/` for everything this package owns
# (config.yaml, toggles.json, daemon socket, indexer log). Files that
# are genuinely shared (the model cache used by either tool's
# embedders, eventually) live at the top level.
#
# Migration. Existing v0.6 claude-rag-hook users had a different layout
# at ~/.config/claude-rag-hook/, ~/.cache/claude-rag-hook/, etc. On
# first invocation those legacy roots are migrated in place to the new
# unified location. The migration is idempotent and best-effort: if
# anything fails, we fall back to creating the new directory empty.

_LEGACY_LEAF = "claude-rag-hook"
_FAMILY_LEAF = "hydra-llm"
_OWN_SUBDIR = "rag-hooks"


def _own_dir(env: str, fallback: Path) -> Path:
    """Resolve our owned subdirectory under the shared hydra-llm family
    root. Performs a one-shot rename of any legacy
    `<xdg>/claude-rag-hook/` into `<xdg>/hydra-llm/rag-hooks/` on first
    call, preserving v0.6 user state.
    """
    root = _xdg(env, fallback)
    family = root / _FAMILY_LEAF
    own = family / _OWN_SUBDIR
    legacy = root / _LEGACY_LEAF
    if not own.exists() and legacy.exists():
        try:
            family.mkdir(parents=True, exist_ok=True)
            legacy.rename(own)
        except OSError:
            pass
    return own


def config_dir() -> Path:
    return _own_dir("XDG_CONFIG_HOME", Path.home() / ".config")


def cache_dir() -> Path:
    return _own_dir("XDG_CACHE_HOME", Path.home() / ".cache")


def state_dir() -> Path:
    return _own_dir("XDG_STATE_HOME", Path.home() / ".local" / "state")


def family_cache_dir() -> Path:
    """The shared `<XDG_CACHE_HOME>/hydra-llm/` root. Used for things
    that are genuinely shared across the family (the model cache),
    not just owned by this package."""
    return _xdg("XDG_CACHE_HOME", Path.home() / ".cache") / _FAMILY_LEAF


# Machine-wide model cache. Shared across the hydra-* family and
# across users on the host: /var/cache/hydra-llm/models/, owned
# root:adm with mode 2775 so any local user can read and the first
# user to download fills it for everyone.
SYSTEM_MODELS_DIR = Path("/var/cache/hydra-llm/models")
LEGACY_SYSTEM_MODELS_DIR = Path("/var/cache/claude-rag-hook/models")


def models_cache_dir() -> Path:
    """Return the directory fastembed (and any future model backends)
    should use as their cache. Order of preference:

      1. /var/cache/hydra-llm/models/ if writable (created by apt
         postinst, mode 2775 root:adm, machine-wide).
      2. /var/cache/claude-rag-hook/models/ if still around from a
         pre-rename install (read-only fallback so existing models
         keep working until the next reinstall does the rename).
      3. ~/.cache/hydra-llm/models/ as a per-user fallback.
    """
    if SYSTEM_MODELS_DIR.is_dir() and os.access(SYSTEM_MODELS_DIR, os.W_OK):
        return SYSTEM_MODELS_DIR
    if LEGACY_SYSTEM_MODELS_DIR.is_dir() and os.access(LEGACY_SYSTEM_MODELS_DIR, os.W_OK):
        return LEGACY_SYSTEM_MODELS_DIR
    user = family_cache_dir() / "models"
    user.mkdir(parents=True, exist_ok=True)
    return user


def config_file() -> Path:
    return config_dir() / "config.yaml"


def stores_registry() -> Path:
    return state_dir() / "stores.json"


def daemon_socket() -> Path:
    return cache_dir() / "embedder.sock"


def daemon_pidfile() -> Path:
    return cache_dir() / "embedder.pid"


def daemon_logfile() -> Path:
    return cache_dir() / "embedder.log"


def claude_settings_file() -> Path:
    return Path.home() / ".claude" / "settings.json"


def find_index(start: Path) -> Path | None:
    """Walk up from `start` and return the path to the nearest index folder.

    Recognises both the unified `.hydra-index/` (new default; also used
    by hydra-llm) and the legacy `.claude-rag-index/` (claude-rag-hook
    v0.6 and earlier). The hook auto-migrates legacy folders to the
    new name on first run, so callers should not see the legacy form
    after that.
    """
    start = start.resolve()
    for d in (start, *start.parents):
        for name in (INDEX_DIR_NAME, LEGACY_CLAUDE_INDEX_DIR_NAME):
            cand = d / name
            if cand.is_dir():
                return cand
    return None


def ensure_dirs() -> None:
    for d in (config_dir(), cache_dir(), state_dir()):
        d.mkdir(parents=True, exist_ok=True)
    # The cache dir holds the daemon socket; tighten permissions.
    try:
        os.chmod(cache_dir(), 0o700)
    except OSError:
        pass
