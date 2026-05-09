"""MCP server (stdio JSON-RPC) for hydra-rag-hooks.

Companion surface to the keyword-triggered hook. The hook is the cheap,
deterministic, zero-token-overhead path: the user types `rag <q>` and
gets retrieval. The MCP server is the model-decides path: Claude calls
`rag_search` when it judges that retrieval would help and the user did
not type the keyword (or typed it but the chunks were thin and Claude
wants a follow-up search with a different query).

Why both:

- The hook is best for known-need lookups. Cheapest per-turn, no MCP
  round trip, zero overhead on prompts that are not retrieval. Saves
  tokens.
- The MCP server is best for follow-up retrieval inside an ongoing
  Claude turn. If the keyword RAG returned three chunks and Claude
  realises a fourth angle would help, it can ask. That is cheaper than
  forcing the user to type `rag <other_q>` and start a new turn from
  scratch.

The two surfaces share retrieval code, embedder, and index store.

Protocol: MCP 2024-11-05 over stdio JSON-RPC 2.0. We hand-roll the
small envelope rather than depending on `mcp` Python SDK because:

- The SDK is not packaged for Debian.
- We only need 3 RPCs (`initialize`, `tools/list`, `tools/call`) plus
  one notification (`notifications/initialized`). Hand-rolling is
  ~80 lines.

The server reads one JSON object per line on stdin, writes one JSON
object per line on stdout. Logs go to stderr (which Claude Code
multiplexes into its own log).

Tools exposed:

- `rag_search(query, scope?, top_k?, tag?)`: retrieval against the
  cwd index (or named scope, or tag-federated) and return chunks with
  file paths and line ranges.
- `rag_status(scope?)`: report the index state for cwd or a named scope.
- `rag_list_stores()`: list every registered store with chunk counts.

The "when to use" guidance for Claude lives in the tool descriptions
themselves. This is the contract the model reads to decide whether
to call.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from . import config as config_mod, paths, registry, retrieval
from .progress import read as read_progress, read_last_run, is_active


PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "hydra-rag-hooks"
SERVER_VERSION = "0.1.1"


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_TOOL_RAG_SEARCH_DESCRIPTION = (
    "Retrieve chunks from the user's local code/document index built by "
    "hydra-rag-hooks. Use this when the user's question would be better "
    "answered by reading specific files in the project, and either:\n"
    "  - the keyword `rag <q>` retrieval already ran but returned thin "
    "    or off-target chunks and you want to follow up with a refined "
    "    query;\n"
    "  - the user did not type `rag` but their question clearly hinges "
    "    on project-specific code, names, or text you have not seen.\n"
    "\n"
    "Do NOT use this tool when:\n"
    "  - the answer is general programming knowledge (use your training);\n"
    "  - you already have the relevant code in conversation context;\n"
    "  - the question is conversational/meta (no project lookup needed).\n"
    "\n"
    "Cost: one round-trip to a local LanceDB index plus a small embedding. "
    "No network. Cheap, but not free; skip if you don't need it.\n"
    "\n"
    "Returns up to top_k chunks with their relative file paths and line "
    "ranges. Treat each block as verbatim text from a file in the indexed "
    "folder; quote line numbers when you cite."
)


_TOOL_RAG_STATUS_DESCRIPTION = (
    "Report whether an index exists for a project folder and how fresh it "
    "is. Useful to confirm that a `rag_search` will find something before "
    "you call it. With no scope, reports on the project containing the "
    "current working directory."
)


_TOOL_RAG_LIST_DESCRIPTION = (
    "List every project folder for which the user has built a "
    "hydra-rag-hooks index, with file/chunk counts and any tags. Useful "
    "when you want to call `rag_search` against a different project than "
    "the one Claude is currently in (pass `scope` in `rag_search`)."
)


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "rag_search",
            "description": _TOOL_RAG_SEARCH_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query. Be specific.",
                    },
                    "scope": {
                        "type": "string",
                        "description": (
                            "Absolute path to a project folder. Defaults to "
                            "the current working directory. Use this to "
                            "search a different project than the cwd."
                        ),
                    },
                    "tag": {
                        "type": "string",
                        "description": (
                            "Federate retrieval across every registered store "
                            "carrying this tag. Mutually exclusive with `scope`. "
                            "Pass 'all' to search every registered store."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of chunks to return. Default 5; max 20.",
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "rag_status",
            "description": _TOOL_RAG_STATUS_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "description": "Absolute path to inspect. Defaults to cwd.",
                    },
                },
            },
        },
        {
            "name": "rag_list_stores",
            "description": _TOOL_RAG_LIST_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
    ]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _resolve_cwd(scope: str | None) -> Path:
    if scope:
        return Path(scope).expanduser().resolve()
    return Path.cwd().resolve()


def _tool_rag_search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return _text_result("rag_search: empty query.", is_error=True)

    top_k = int(args.get("top_k") or 5)
    top_k = max(1, min(20, top_k))
    tag = args.get("tag")
    scope = args.get("scope")

    if tag:
        indexes = retrieval.resolve_indexes(Path.cwd(), str(tag))
        scope_desc = f"tag '{tag}'"
    else:
        cwd = _resolve_cwd(scope if isinstance(scope, str) else None)
        idx = paths.find_index(cwd)
        indexes = [idx] if idx else []
        scope_desc = str(cwd)

    if not indexes:
        return _text_result(
            f"rag_search: no index found for {scope_desc}. Tell the user to "
            f"type `rag <question>` once in that folder to auto-index it, "
            f"then retry.",
            is_error=False,
        )

    cfg = config_mod.load()
    try:
        hits = retrieval.retrieve(query, indexes, top_k=top_k, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        return _text_result(f"rag_search: error: {type(e).__name__}: {e}", is_error=True)

    if not hits:
        return _text_result(
            f"rag_search: no relevant chunks found in {scope_desc} for query: {query!r}.",
            is_error=False,
        )

    lines = [
        f"Retrieved {len(hits)} chunk(s) from hydra-rag-hooks local index "
        f"({scope_desc}). Each block is verbatim text from a file; treat it "
        f"as ground truth and cite line ranges when you quote.",
        "",
    ]
    for h in hits:
        if h.start_line and h.end_line:
            lines.append(f"--- {h.rel}:{h.start_line}-{h.end_line} ({h.kind}) ---")
        else:
            lines.append(f"--- {h.rel} ({h.kind}) ---")
        lines.append(h.text)
        lines.append("")
    return _text_result("\n".join(lines), is_error=False)


def _index_is_populated(index_dir: Path) -> bool:
    """A `.claude-rag-index/` directory may exist without a populated
    LanceDB table (an aborted indexing attempt leaves the dir but no
    .lance subdirectory). Same check the hook uses; mirrored here so
    rag_status doesn't say "ready" when there is no queryable data."""
    if not index_dir.is_dir():
        return False
    try:
        for entry in index_dir.iterdir():
            if entry.is_dir() and entry.name.endswith(".lance"):
                return True
    except OSError:
        return False
    return False


def _tool_rag_status(args: dict[str, Any]) -> dict[str, Any]:
    scope = args.get("scope")
    cwd = _resolve_cwd(scope if isinstance(scope, str) else None)
    idx = paths.find_index(cwd)
    if idx is None:
        return _text_result(
            f"rag_status: no hydra-rag-hooks index found in or above {cwd}. "
            f"The user can build one by typing `rag <question>` in that folder.",
            is_error=False,
        )
    project = idx.parent
    prog = read_progress(idx)
    last = read_last_run(idx)
    active = is_active(idx)
    populated = _index_is_populated(idx)
    parts = [f"rag_status: index for {project}"]
    if active:
        parts.append(f"  state: {prog.state} (in progress)")
        if prog.files_total:
            parts.append(f"  progress: {prog.files_done}/{prog.files_total} files")
    elif prog.state == "error":
        parts.append(f"  state: error ({prog.message})")
    elif not populated:
        parts.append(
            "  state: empty (directory exists but no LanceDB table; "
            "indexing was aborted before any rows were written). "
            "Tell the user to type `rag <question>` to rebuild."
        )
    else:
        parts.append("  state: ready")
    if last is not None:
        parts.append(f"  chunks: {last.chunks_added}")
        parts.append(f"  files: {last.files_indexed or last.files_total}")
    return _text_result("\n".join(parts), is_error=False)


def _tool_rag_list_stores(args: dict[str, Any]) -> dict[str, Any]:
    entries = registry.load()
    if not entries:
        return _text_result(
            "rag_list_stores: no registered stores. The user has not "
            "indexed any project yet.",
            is_error=False,
        )
    lines = ["rag_list_stores: registered stores:"]
    for e in entries:
        tags = ",".join(sorted(e.tags)) if e.tags else "-"
        lines.append(f"  {e.path}  (tags: {tags})")
    return _text_result("\n".join(lines), is_error=False)


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------


def _text_result(text: str, is_error: bool) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": bool(is_error),
    }


_TOOL_DISPATCH = {
    "rag_search": _tool_rag_search,
    "rag_status": _tool_rag_status,
    "rag_list_stores": _tool_rag_list_stores,
}


def _handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC request. Return the response, or None for notifications."""
    method = request.get("method")
    rid = request.get("id")
    params = request.get("params") or {}

    # Notifications (no id) never get a reply.
    if method == "notifications/initialized":
        return None
    if method and method.startswith("notifications/"):
        return None

    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "tools/list":
        return _ok(rid, {"tools": _tool_definitions()})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = _TOOL_DISPATCH.get(name)
        if fn is None:
            return _err(rid, -32601, f"unknown tool: {name}")
        try:
            result = fn(args if isinstance(args, dict) else {})
        except Exception as e:  # noqa: BLE001
            return _err(rid, -32603, f"{type(e).__name__}: {e}")
        return _ok(rid, result)
    if method == "ping":
        return _ok(rid, {})

    return _err(rid, -32601, f"method not found: {method}")


def _ok(rid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def serve(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    print(
        f"hydra-rag-mcp v{SERVER_VERSION} ready (stdio).",
        file=sys.stderr, flush=True,
    )
    while True:
        line = stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"hydra-rag-mcp: invalid JSON: {e}", file=sys.stderr, flush=True)
            continue
        if isinstance(req, list):
            # Batch (rare in MCP). Process each.
            replies = [r for r in (_handle(item) for item in req) if r is not None]
            if replies:
                stdout.write(json.dumps(replies) + "\n")
                stdout.flush()
            continue
        if not isinstance(req, dict):
            continue
        reply = _handle(req)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    # Honour an env-var kill switch. When users toggle MCP off via `crh
    # mcp off`, the per-user MCP server entry in ~/.claude.json points at
    # this binary with CLAUDE_RAG_MCP_DISABLED=1. We exit cleanly so
    # Claude Code marks the server as failed-but-recoverable rather than
    # spinning on it.
    if os.environ.get("CLAUDE_RAG_MCP_DISABLED") == "1":
        print("hydra-rag-mcp: disabled by user (CLAUDE_RAG_MCP_DISABLED=1).",
              file=sys.stderr, flush=True)
        return 0
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
