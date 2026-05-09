"""crh tag / crh untag - manage tags on a registered store.

Tags drive the `rag@<tag>: <q>` federated retrieval syntax. Multiple
stores can share a tag (eg. tag every active project with "work")
and `rag@work: <q>` retrieves across all of them.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import registry
from . import _common

_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,30}$")


def _validate_tag(tag: str) -> str | None:
    """Return error message if tag is invalid, None if OK.

    Tags must be lowercase, alphanumeric + dot/underscore/dash, 1-31 chars.
    Reserved: 'all' is special in `rag@all:` syntax.
    """
    if not _TAG_RE.match(tag):
        return (
            f"invalid tag {tag!r}: must be lowercase alphanumeric + "
            f"dot/underscore/dash, 1-31 chars, starts with letter or digit"
        )
    if tag == "all":
        return "tag 'all' is reserved for `rag@all:` (every store); pick another"
    return None


def _find_entry(path: Path) -> registry.StoreEntry | None:
    for e in registry.load():
        if Path(e.path).resolve() == path:
            return e
    return None


def run_tag(args) -> int:
    err = _validate_tag(args.tag)
    if err:
        _common.stderr(f"crh tag: {err}")
        return 2

    path = _common.resolve_path(args.path)
    entry = _find_entry(path)
    if entry is None:
        _common.stderr(
            f"crh tag: {path} is not a registered store. "
            f"`crh ls` to see registered stores, or `crh index {path}` first."
        )
        return 1

    if args.tag in entry.tags:
        print(f"(tag {args.tag!r} already on {path})")
        return 0
    entry.tags.append(args.tag)
    registry.upsert(entry)
    print(f"tagged {path} with {args.tag!r}")
    return 0


def run_untag(args) -> int:
    path = _common.resolve_path(args.path)
    entry = _find_entry(path)
    if entry is None:
        _common.stderr(f"crh untag: {path} is not a registered store.")
        return 1
    if args.tag not in entry.tags:
        print(f"(tag {args.tag!r} not on {path})")
        return 0
    entry.tags.remove(args.tag)
    registry.upsert(entry)
    print(f"untagged {path}: removed {args.tag!r}")
    return 0
