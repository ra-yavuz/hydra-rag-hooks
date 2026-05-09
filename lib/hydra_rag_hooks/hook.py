"""UserPromptSubmit hook entrypoint.

Behavior on `rag <q>` / `rag: <q>` / `/rag <q>` / `rag@<tag>: <q>`:

1. If an index already exists in or above cwd:
     - Retrieve top-K chunks for the query under a wall-clock timeout
       (config: retrieval.timeout_seconds, default 8s). On timeout, fall
       through with a stderr note. Claude is never blocked indefinitely.
     - In the background, fire off an incremental refresh if the index
       is past its refresh-throttle interval. Non-blocking.

2. If no index exists:
     - Run auto-index gates. If they pass, fork-detach the indexer,
       persist the user's query as a "queued" replay hint, and pass
       through to Claude with a small note so Claude can answer from
       training knowledge for this turn. On any subsequent prompt,
       once indexing has finished, the hook prepends a one-time
       "your earlier `rag <q>` is ready - type that again" banner.

Bare `rag` / `/rag` / `rag status` is a status command. It uses the
documented `decision: "block"` envelope so Claude Code ends the turn
without invoking the model:

   - If no index exists: kick off indexing in the background and tell
     the user. Block the model.
   - If indexing is in progress: report live progress. Block the model.
   - If index is ready: report stats. Block the model.
   - If last attempt errored: report the error and recovery hint.
     Block the model.

Indexing-banner: when a non-rag prompt is submitted while an indexing
job is active, the hook prepends a small heads-up to stdout so Claude
mentions it. This way the user is never in the dark about a running
detached indexer.

Completion-banner: when a non-rag prompt is submitted *after* indexing
finishes and the user's earlier query is queued for replay, the hook
prepends a one-time "indexing complete; your earlier rag <q> is ready"
note.

Non-trigger prompts otherwise produce no output and exit 0. The hook is
fail-soft: any internal exception is logged on stderr and exits 0 so the
user always gets a response from Claude.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import sys
import time
from pathlib import Path

from . import (
    auto_index,
    config as config_mod,
    mcp_register,
    migrate,
    paths,
    progress as progress_mod,
    retrieval,
    runner,
    toggles,
    trigger,
)


def _emit_block(reason: str) -> int:
    """Reply with a `decision: block` envelope so Claude Code ends the
    turn without invoking the model. The `reason` field is shown to the
    user in place of a Claude response.
    """
    sys.stdout.write(json.dumps({
        "decision": "block",
        "reason": reason,
    }))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def _scope_hash(scope: Path) -> str:
    """Stable short hash of an absolute path, for naming per-scope cache
    files (queued query, last-seen marker)."""
    return hashlib.sha1(str(scope.resolve()).encode("utf-8")).hexdigest()[:16]


def _queued_query_path(scope: Path) -> Path:
    return paths.cache_dir() / f"{_scope_hash(scope)}.queued_query"


def _write_queued_query(scope: Path, query: str) -> None:
    p = _queued_query_path(scope)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(query, encoding="utf-8")
    except OSError:
        pass


def _read_queued_query(scope: Path) -> str | None:
    p = _queued_query_path(scope)
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except (FileNotFoundError, OSError):
        return None


def _clear_queued_query(scope: Path) -> None:
    try:
        _queued_query_path(scope).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _start_indexing(scope: Path) -> None:
    """Fork-detach an indexer for `scope`. Caller is responsible for
    deciding whether to start one (e.g. checking is_active)."""
    runner.fork_detach_index(scope, kind="indexing")


def run(stdin_text: str, cwd: Path | None = None) -> int:
    try:
        envelope = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        # Some Claude Code versions / wrappers may pass raw text. Treat as prompt.
        envelope = {"prompt": stdin_text}

    prompt = envelope.get("prompt") or ""
    if not isinstance(prompt, str):
        return 0

    env_cwd = envelope.get("cwd") or envelope.get("working_directory")
    if cwd is None:
        cwd = Path(env_cwd) if env_cwd else Path.cwd()

    # One-shot migration of any legacy .claude-rag-index/ folder in this
    # tree to the unified .hydra-index/ name. Idempotent; cheap when
    # there's nothing to move. Operators can disable with the env var
    # HYDRA_RAG_HOOKS_SKIP_MIGRATIONS=1.
    if not migrate.env_says_skip():
        try:
            migrate.migrate_index_folder(cwd)
        except Exception:  # noqa: BLE001
            pass

    # Idempotently sync the MCP server entry in ~/.claude.json. Cheap;
    # only writes the file when state would change. Honours the user
    # toggle so `crh mcp off` flips the disabled flag rather than
    # ripping the entry out and adding it back next turn.
    try:
        mcp_register.ensure_registered(disabled=not toggles.mcp_enabled())
    except Exception:  # noqa: BLE001
        # Per fail-soft contract: never break the user's prompt because
        # the registration helper hit an unexpected file shape.
        pass
    try:
        mcp_register.ensure_slash_command()
    except Exception:  # noqa: BLE001
        pass

    cfg = config_mod.load()
    triggers = config_mod.triggers(cfg)
    lax = bool(cfg.get("lax_trigger", default=False))
    match = trigger.parse(prompt, triggers, lax=lax)

    # Auto-rag: when the user has toggled it on, every prompt that did
    # NOT already match a trigger and is not a slash command is treated
    # as if they had typed `rag <prompt>`. The point is to spare them
    # the keyword once they have decided "this whole conversation is
    # about my project". Slash commands and bare-empty prompts are
    # never auto-promoted; the user clearly meant something else.
    if match is None and toggles.auto_rag_enabled() and _eligible_for_auto_rag(prompt):
        match = trigger.TriggerMatch(query=prompt.strip(), tag=None)

    if match is None:
        # Not a RAG turn. If a background index job is running for this
        # tree, surface a short heads-up so Claude (and the user) know.
        _maybe_emit_indexing_banner(cwd)
        return 0

    if match.command == "status":
        return _emit_status(cwd)

    # Tagged retrieval (rag@all:, rag@<tag>:) bypasses auto-index entirely:
    # the user has explicitly named the scope, so it's already on them to
    # have indexed it.
    if match.tag is not None:
        indexes = retrieval.resolve_indexes(cwd, match.tag)
        if not indexes:
            scope_desc = "any registered store" if match.tag == "all" else f"tag '{match.tag}'"
            print(f"hydra-rag-hooks: no index found for {scope_desc}.",
                  file=sys.stderr, flush=True)
            return 0
        return _emit_retrieval(match.query, indexes, cfg)

    # Untagged: prefer the existing index in or above cwd, else auto-index.
    existing = paths.find_index(cwd)
    if existing is not None and _index_is_populated(existing):
        # Index is there and has data. Retrieve, then maybe refresh in
        # the background.
        scope = existing.parent
        try:
            runner.maybe_refresh(scope)
        except Exception as e:
            # Background refresh failures must not break retrieval.
            print(f"hydra-rag-hooks: background refresh skipped ({e}).",
                  file=sys.stderr, flush=True)
        return _emit_retrieval(match.query, [existing], cfg)

    if existing is not None:
        # Directory exists but is empty (e.g. last indexing attempt
        # failed before writing any rows). Treat the same as "no index"
        # below, so the error-surfacing branch can speak up.
        scope_for_error = existing.parent
        last = progress_mod.read(existing)
        if last.state == "error":
            print(
                f"hydra-rag-hooks: previous indexing of {scope_for_error} "
                f"failed: {last.message}\n"
                f"  See {paths.cache_dir() / 'indexer.log'} for the traceback.\n"
                f"  Common fix: pip install --user fastembed lancedb pyarrow\n"
                f"  Then delete {existing}/.progress to retry.",
                file=sys.stderr, flush=True,
            )
            return 0

    # No index. Decide whether we can auto-index.
    decision = auto_index.decide(cwd)
    if not decision.allow:
        print(f"hydra-rag-hooks: {decision.reason}", file=sys.stderr, flush=True)
        return 0

    # Auto-index allowed. Is there already a job in progress for this scope?
    scope = decision.scope
    assert scope is not None
    index_dir = scope / paths.INDEX_DIR_NAME
    if progress_mod.is_active(index_dir):
        prog = progress_mod.read(index_dir)
        msg = prog.as_human() or f"hydra-rag-hooks: indexing {scope} in progress."
        print(f"{msg}. Type `rag` to check progress.",
              file=sys.stderr, flush=True)
        return 0

    # Did a previous attempt fail? Surface the error so the user can act on
    # it (typically: install fastembed, check config). Do not re-trigger
    # indexing automatically; the same error will just repeat.
    last = progress_mod.read(index_dir)
    if last.state == "error":
        print(
            f"hydra-rag-hooks: previous indexing attempt of {scope} failed: "
            f"{last.message}\n"
            f"  See {paths.cache_dir() / 'indexer.log'} for the traceback.\n"
            f"  Common fix: pip install --user fastembed lancedb pyarrow\n"
            f"  Then delete {index_dir}/.progress to retry.",
            file=sys.stderr, flush=True,
        )
        return 0

    # Kick off a fresh indexing job and tell the user. The user's query
    # is persisted as a "queued" replay hint so a subsequent prompt can
    # surface a one-time "your earlier rag <q> is ready" banner once
    # indexing completes.
    _start_indexing(scope)
    _write_queued_query(scope, match.query)
    print(
        f"hydra-rag-hooks: indexing {scope} in the background. "
        f"This is a one-time setup per project. "
        f"Type `rag` (alone) any time to check progress.",
        file=sys.stderr, flush=True,
    )
    sys.stdout.write(
        f"[hydra-rag-hooks] heads-up for Claude: the user typed "
        f"`rag {match.query}` but no index exists for this folder yet. "
        f"Indexing has just started in the background; retrieval will "
        f"be available on the next `rag <q>` once it completes. Answer "
        f"the user's question from your training knowledge for this "
        f"turn, and tell them their earlier query will be ready to "
        f"replay shortly.\n\n"
    )
    sys.stdout.flush()
    return 0


def _eligible_for_auto_rag(prompt: str) -> bool:
    """Decide whether a non-trigger prompt should be auto-promoted to a
    `rag` query when the auto_rag toggle is on.

    Skip slash commands (the user is invoking a built-in or custom
    skill), empty prompts, and very short prompts that are almost
    certainly conversational ("ok", "thanks"). Everything else gets
    retrieval. Cheap-to-compute heuristic, deliberately permissive:
    the worst case of a false positive is one extra retrieval round
    trip.
    """
    s = prompt.strip()
    if not s:
        return False
    if s.startswith("/"):
        return False
    if len(s) < 4:
        return False
    return True


def _index_is_populated(index_dir: Path) -> bool:
    """Cheap check: does the index actually contain a LanceDB table?

    Just having a `.claude-rag-index/` directory is not enough; an
    aborted indexing attempt leaves the directory but no `chunks.lance/`
    inside it. Treating that as "index ready" leads to empty retrievals
    and confused users.
    """
    if not index_dir.is_dir():
        return False
    try:
        for entry in index_dir.iterdir():
            # LanceDB writes one .lance subdirectory per table.
            if entry.is_dir() and entry.name.endswith(".lance"):
                return True
    except OSError:
        return False
    return False


# ---------------------------------------------------------------------------
# Retrieval (with timeout)
# ---------------------------------------------------------------------------


def _retrieve_worker(query: str, index_paths: list[str], top_k: int, cfg_data: dict, q):
    """Subprocess target: do the retrieval and put the result on the queue.

    Runs in a child process so the parent can enforce a wall-clock cap
    via process.join(timeout). Cold-start embedder loads can otherwise
    hold Claude for tens of seconds.
    """
    try:
        from . import config as _cfg, retrieval as _ret  # re-import in child
        cfg_obj = _cfg.Config(data=cfg_data)
        hits = _ret.retrieve(
            query, [Path(p) for p in index_paths], top_k=top_k, cfg=cfg_obj,
        )
        q.put(("ok", [
            {
                "rel": h.rel,
                "start_line": h.start_line,
                "end_line": h.end_line,
                "kind": h.kind,
                "text": h.text,
            }
            for h in hits
        ]))
    except Exception as e:  # noqa: BLE001
        q.put(("error", f"{type(e).__name__}: {e}"))


def _emit_retrieval(query: str, indexes: list[Path], cfg) -> int:
    top_k = int(cfg.get("top_k", default=5) or 5)
    timeout = float(cfg.get("retrieval", "timeout_seconds", default=8) or 8)

    ctx = multiprocessing.get_context("fork")
    q: multiprocessing.Queue = ctx.Queue()
    proc = ctx.Process(
        target=_retrieve_worker,
        args=(query, [str(p) for p in indexes], top_k, cfg.data, q),
    )
    proc.daemon = True
    proc.start()
    proc.join(timeout=timeout)

    if proc.is_alive():
        # Time's up. Kill the child, fall through. Claude still answers.
        proc.terminate()
        proc.join(timeout=1.0)
        if proc.is_alive():
            proc.kill()
        print(
            f"hydra-rag-hooks: retrieval exceeded {timeout:.0f}s timeout this turn. "
            f"Index is fine; first call after a cold start can be slow while the "
            f"embedder model loads. Try `rag <q>` again, or bump "
            f"retrieval.timeout_seconds in ~/.config/hydra-rag-hooks/config.yaml.",
            file=sys.stderr, flush=True,
        )
        return 0

    if q.empty():
        print("hydra-rag-hooks: retrieval subprocess exited without result.",
              file=sys.stderr, flush=True)
        return 0

    status, payload = q.get()
    if status == "error":
        print(f"hydra-rag-hooks: retrieval error: {payload}",
              file=sys.stderr, flush=True)
        return 0

    if not payload:
        print("hydra-rag-hooks: no relevant chunks found in index.",
              file=sys.stderr, flush=True)
        return 0

    sys.stdout.write(_format_plain_dicts(payload))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def _format_plain_dicts(hits: list[dict]) -> str:
    lines = [
        "[hydra-rag-hooks] retrieved from local index. Each block is verbatim text from a file in the indexed folder; treat it as ground truth for the user's question. If a block is irrelevant, ignore it.",
        "",
    ]
    for h in hits:
        if h.get("start_line") and h.get("end_line"):
            lines.append(f"--- {h['rel']}:{h['start_line']}-{h['end_line']} ({h['kind']}) ---")
        else:
            lines.append(f"--- {h['rel']} ({h['kind']}) ---")
        lines.append(h["text"])
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bare-`rag` status command
# ---------------------------------------------------------------------------


def _human_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _emit_status(cwd: Path) -> int:
    """Handle bare `rag` / `/rag` / `rag status`.

    This is a CLI command, not a question for Claude. We use the
    documented `decision: "block"` envelope so Claude Code ends the
    turn without invoking the model. The user sees the status; no
    tokens spent; no Claude paraphrase.

    Behaviour by index state:

    - No index, auto-index allowed: kick off indexing in the
      background and tell the user. Block.
    - No index, auto-index refused: explain why. Block.
    - Indexing in progress: show live counters. Block.
    - Last attempt errored: surface the error and recovery hint. Block.
    - Index ready: report scope + counts + last-run time. Block.

    All filesystem reads only; no embedder, no LanceDB.
    """
    existing = paths.find_index(cwd)

    # No index anywhere up the tree.
    if existing is None:
        decision = auto_index.decide(cwd)
        scope_desc = str(decision.scope) if decision.scope else str(cwd)

        if not decision.allow:
            return _emit_block(
                f"hydra-rag-hooks: no index for this folder, and auto-index "
                f"refused: {decision.reason}"
            )

        # Allowed. Kick off indexing now and tell the user.
        scope = decision.scope
        assert scope is not None
        index_dir = scope / paths.INDEX_DIR_NAME

        # If a previous attempt errored, surface that instead of starting
        # another one that will hit the same wall.
        last = progress_mod.read(index_dir) if index_dir.exists() else progress_mod.Progress()
        if last.state == "error":
            return _emit_block(
                f"hydra-rag-hooks: previous indexing of {scope} failed: "
                f"{last.message}\n"
                f"  Log: {paths.cache_dir() / 'indexer.log'}\n"
                f"  Common fix: pip install --user fastembed lancedb pyarrow\n"
                f"  Then delete {index_dir}/.progress to retry."
            )

        if not progress_mod.is_active(index_dir):
            _start_indexing(scope)
        return _emit_block(
            f"hydra-rag-hooks: indexing {scope_desc} in the background. "
            f"This is a one-time setup. Type `rag` again to check progress, "
            f"or type `rag <question>` once it finishes to retrieve."
        )

    scope = existing.parent
    prog = progress_mod.read(existing)
    last_run = progress_mod.read_last_run(existing)
    is_active = progress_mod.is_active(existing)
    log_path = paths.cache_dir() / "indexer.log"

    if is_active:
        elapsed = _human_duration(time.time() - prog.started_at)
        verb = "indexing" if prog.state == "indexing" else "refreshing"
        counter = (
            f"{prog.files_done}/{prog.files_total} files"
            if prog.files_total > 0
            else f"{prog.files_done} files so far"
        )
        return _emit_block(
            f"hydra-rag-hooks: {verb} {scope}\n"
            f"  progress: {counter}\n"
            f"  elapsed: {elapsed}\n"
            f"  log: {log_path}"
        )

    if prog.state == "error":
        return _emit_block(
            f"hydra-rag-hooks: last indexing of {scope} failed: {prog.message}\n"
            f"  log: {log_path}\n"
            f"  recover: pip install --user fastembed lancedb pyarrow, "
            f"then delete {existing}/.progress to retry."
        )

    # Idle.
    populated = _index_is_populated(existing)
    if last_run is not None:
        ago = _human_duration(time.time() - last_run.finished_at)
        files = last_run.files_indexed or last_run.files_total
        return _emit_block(
            f"hydra-rag-hooks: index ready for {scope}\n"
            f"  chunks: {last_run.chunks_added}\n"
            f"  files: {files}\n"
            f"  last {last_run.kind}: {ago} ago "
            f"(took {_human_duration(last_run.elapsed_seconds)})\n"
            f"  type `rag <question>` to retrieve."
        )
    if populated:
        return _emit_block(
            f"hydra-rag-hooks: index ready for {scope} (no run stats; built "
            f"by an older version or another tool). Type `rag <question>` "
            f"to retrieve."
        )
    return _emit_block(
        f"hydra-rag-hooks: index folder at {existing} exists but is empty. "
        f"Type `rag <question>` to (re)build."
    )


# ---------------------------------------------------------------------------
# Indexing-in-progress banner on non-rag prompts
# ---------------------------------------------------------------------------


def _maybe_emit_indexing_banner(cwd: Path) -> None:
    """Surface background-indexer state on non-rag prompts.

    Two banners can fire here, both via stdout (Claude sees them and
    will mention to the user):

    1. **Still-indexing banner.** Active indexing job for this tree,
       state == "indexing" (initial build, not 5-min refresh).

    2. **Completion banner.** Indexing has finished and there is a
       queued query waiting to be replayed (the user's earlier
       `rag <q>` that arrived before the index existed). Fires once,
       then clears the queued file so the next prompt is silent.
    """
    existing = paths.find_index(cwd)
    if existing is None:
        return

    scope = existing.parent

    # Case 1: still indexing.
    if progress_mod.is_active(existing):
        prog = progress_mod.read(existing)
        if prog.state == "indexing":
            elapsed = _human_duration(time.time() - prog.started_at)
            counter = (
                f"{prog.files_done}/{prog.files_total} files"
                if prog.files_total > 0
                else f"{prog.files_done} files so far"
            )
            sys.stdout.write(
                f"[hydra-rag-hooks] heads-up for Claude: still indexing "
                f"{scope} in the background ({counter}, {elapsed} elapsed). "
                f"Retrieval via `rag <q>` will work as soon as it finishes. "
                f"The user can type `rag` alone any time for live status.\n\n"
            )
            sys.stdout.flush()
        return

    # Case 2: indexing has finished and a query is queued for replay.
    queued = _read_queued_query(scope)
    if queued and _index_is_populated(existing):
        sys.stdout.write(
            f"[hydra-rag-hooks] indexing complete for {scope}. The user's "
            f"earlier query `rag {queued}` is now ready to retrieve - "
            f"please tell them to type that again to use the freshly-built "
            f"index. (This banner only fires once.)\n\n"
        )
        sys.stdout.flush()
        _clear_queued_query(scope)


def main(argv: list[str] | None = None) -> int:
    text = sys.stdin.read() if not sys.stdin.isatty() else ""
    return run(text)


if __name__ == "__main__":
    raise SystemExit(main())
