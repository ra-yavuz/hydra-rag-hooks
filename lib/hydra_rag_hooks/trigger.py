"""Trigger parser.

Decides whether a user prompt is a RAG turn and, if so, extracts the
query text, any tag scope (e.g. 'rag@work: ...'), or a bare-form command.

Supported forms (case-insensitive, leading whitespace tolerated):

    rag: <text>
    /rag <text>
    rag <text>            (lax form, on by default; toggle with `lax_trigger`)
    rag@<tag>: <text>     (federated across tagged stores)
    rag@all: <text>       (federated across all registered stores)

Bare forms (no query text) are recognised as commands rather than
queries:

    rag                   -> command="status"
    /rag                  -> command="status"
    rag status            -> command="status"
    rag:                  -> command="status"

Bare commands never run retrieval; they print a one-shot status report
and return immediately. This keeps the hook non-blocking when the user
just wants to know what's going on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class TriggerMatch:
    query: str
    tag: str | None  # None means "current folder"; "all" means every store; otherwise a tag
    command: str | None = None  # None for normal queries; "status" for bare-form invocations


# Order matters: longer / more specific patterns first.
_PATTERNS_TAGGED = [
    re.compile(r"^\s*rag@([A-Za-z0-9_.\-]+)\s*:\s*(.*)$", re.IGNORECASE | re.DOTALL),
    re.compile(r"^\s*/rag@([A-Za-z0-9_.\-]+)\s+(.*)$", re.IGNORECASE | re.DOTALL),
]

_PATTERNS_PLAIN = {
    "rag:": re.compile(r"^\s*rag\s*:\s*(.*)$", re.IGNORECASE | re.DOTALL),
    "/rag": re.compile(r"^\s*/rag(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL),
}

_LAX = re.compile(r"^\s*rag\s+(.*)$", re.IGNORECASE | re.DOTALL)
_BARE = re.compile(r"^\s*/?rag\s*:?\s*$", re.IGNORECASE)
_STATUS_WORD = re.compile(r"^\s*/?rag\s+status\s*$", re.IGNORECASE)


def _status_match() -> TriggerMatch:
    return TriggerMatch(query="", tag=None, command="status")


def parse(prompt: str, triggers: list[str], lax: bool = False) -> TriggerMatch | None:
    if not prompt:
        return None

    # Bare forms first: `rag`, `/rag`, `rag:`, `rag status`.
    if _BARE.match(prompt) or _STATUS_WORD.match(prompt):
        return _status_match()

    for pat in _PATTERNS_TAGGED:
        m = pat.match(prompt)
        if m:
            tag = m.group(1).strip().lower() or None
            query = (m.group(2) or "").strip()
            if not query:
                return None
            return TriggerMatch(query=query, tag=tag)

    enabled = {t.strip().lower() for t in triggers}
    for key, pat in _PATTERNS_PLAIN.items():
        if key not in enabled:
            continue
        m = pat.match(prompt)
        if m:
            query = ((m.group(1) or "") if m.lastindex else "").strip()
            if not query:
                return None
            return TriggerMatch(query=query, tag=None)

    if lax:
        m = _LAX.match(prompt)
        if m:
            query = (m.group(1) or "").strip()
            if not query:
                return None
            return TriggerMatch(query=query, tag=None)

    return None
