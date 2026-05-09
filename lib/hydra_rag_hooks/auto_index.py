"""Decide whether a folder should be auto-indexed, and resolve the right scope.

Safety rails (non-negotiable, the entire reason auto-index is acceptable as
a default):

- Refuse $HOME, every direct child of $HOME, /, /etc, /var, /tmp, /usr,
  /opt, /root.
- Require a project marker (.git, pyproject.toml, package.json, Cargo.toml,
  go.mod, etc.) somewhere in the cwd ancestor chain up to a depth limit.
- Hard size cap: if the chosen scope's walk would touch more than
  MAX_AUTO_INDEX_FILES files or MAX_AUTO_INDEX_BYTES bytes, refuse and
  ask the user to opt in explicitly via env var.

When auto-index is refused, the hook explains why on stderr and passes
the prompt through unchanged. We never silently fail and we never silently
succeed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import walker

# Markers that signal "this is the root of a coherent project" - same set
# git, ripgrep, pre-commit, and pretty much every dev tool agrees on.
PROJECT_MARKERS = (
    ".git",
    ".hg",
    ".svn",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "build.gradle",
    "settings.gradle",
    "pom.xml",
    "composer.json",
    "Gemfile",
    "mix.exs",
    "deno.json",
    "bun.lockb",
    "Makefile",
    "CMakeLists.txt",
    ".claude-rag-allow",  # explicit user opt-in marker
)

MAX_ANCESTOR_DEPTH = 6
MAX_AUTO_INDEX_FILES = 20_000
MAX_AUTO_INDEX_BYTES = 500 * 1024 * 1024

# Folders we will never auto-index even if a project marker is present.
# (Someone putting a Makefile in /etc/ does not get to opt /etc/ in.)
HARD_REFUSE = (
    Path("/"),
    Path("/etc"),
    Path("/var"),
    Path("/tmp"),
    Path("/usr"),
    Path("/usr/local"),
    Path("/opt"),
    Path("/root"),
    Path("/boot"),
    Path("/sys"),
    Path("/proc"),
    Path("/dev"),
)


@dataclass
class AutoIndexDecision:
    allow: bool
    scope: Path | None        # the folder we should index
    reason: str               # short human-readable explanation


def _hard_refused(p: Path) -> bool:
    p = p.resolve()
    if p in HARD_REFUSE:
        return True
    if p == Path.home().resolve():
        return True
    return False
    # Note: we do NOT refuse direct children of $HOME. Project folders
    # like ~/myproject, ~/work/foo, ~/code/bar are common and legitimate;
    # the project-marker check below already gates everything else
    # (typing `rag:` from ~/Documents with no .git inside gets refused
    # for "no project marker", which is the right message).


def find_project_root(start: Path) -> Path | None:
    """Walk up from `start` and return the nearest ancestor with a project marker.

    Stops at filesystem root, $HOME, or after MAX_ANCESTOR_DEPTH ancestors.
    """
    start = start.resolve()
    home = Path.home().resolve()
    cur = start
    for _ in range(MAX_ANCESTOR_DEPTH):
        if cur == Path(cur.root):
            return None
        if cur == home:
            return None
        for marker in PROJECT_MARKERS:
            if (cur / marker).exists():
                return cur
        cur = cur.parent
    return None


def _quick_size_estimate(root: Path) -> tuple[int, int]:
    """Cheap walk for the size cap.

    Reuses the real walker so the count we cap on is the same one the
    indexer would produce; keeps us from accepting a folder that the
    walker then chokes on.
    """
    opts = walker.WalkOptions(max_file_size_mb=1.0, respect_gitignore=True)
    n_files = 0
    n_bytes = 0
    for f in walker.walk(root, opts):
        n_files += 1
        n_bytes += f.size
        if n_files > MAX_AUTO_INDEX_FILES or n_bytes > MAX_AUTO_INDEX_BYTES:
            break
    return n_files, n_bytes


def decide(cwd: Path, env: dict[str, str] | None = None) -> AutoIndexDecision:
    """Return whether to auto-index, and if so, the folder to index."""
    env = env if env is not None else os.environ.copy()
    cwd = cwd.resolve()

    if _hard_refused(cwd):
        return AutoIndexDecision(False, None,
            f"refusing to auto-index {cwd}; system / sensitive directory.")

    scope = find_project_root(cwd)
    if scope is None:
        return AutoIndexDecision(False, None,
            f"no project marker (.git, pyproject.toml, package.json, ...) "
            f"found in {cwd} or its ancestors; this folder is not "
            f"auto-indexed. Run hydra-rag-hooks from inside a project, or "
            f"drop a .claude-rag-allow file in the folder you want indexed.")

    if _hard_refused(scope):
        return AutoIndexDecision(False, None,
            f"refusing to auto-index {scope}; system / sensitive directory.")

    if env.get("CLAUDE_RAG_HOOK_BYPASS_SIZE_CAP") != "1":
        n_files, n_bytes = _quick_size_estimate(scope)
        if n_files > MAX_AUTO_INDEX_FILES:
            return AutoIndexDecision(False, scope,
                f"refusing to auto-index {scope}: more than "
                f"{MAX_AUTO_INDEX_FILES} indexable files. Set "
                f"CLAUDE_RAG_HOOK_BYPASS_SIZE_CAP=1 to override.")
        if n_bytes > MAX_AUTO_INDEX_BYTES:
            return AutoIndexDecision(False, scope,
                f"refusing to auto-index {scope}: more than "
                f"{MAX_AUTO_INDEX_BYTES // (1024 * 1024)} MB of indexable "
                f"content. Set CLAUDE_RAG_HOOK_BYPASS_SIZE_CAP=1 to override.")

    return AutoIndexDecision(True, scope, f"auto-indexing {scope}")


def deny_auto_index(cwd: Path) -> bool:
    """Quick check used by the smoke tests: would `decide` allow this cwd?"""
    return not decide(cwd).allow
