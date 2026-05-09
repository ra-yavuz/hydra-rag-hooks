"""UserPromptSubmit hook for OpenAI's Codex CLI.

Codex CLI added a UserPromptSubmit lifecycle hook in v0.116.0 (March
2026). Schema differs from Claude Code's hook in two places:

- Stdin envelope. Codex sends a JSON object with `prompt`, `cwd`,
  `session_id`, `hook_event_name`, and `turn_id` fields.
- Stdout response. To inject extra context, Codex expects:

      {
        "hookSpecificOutput": {
          "hookEventName": "UserPromptSubmit",
          "additionalContext": "<text>"
        }
      }

  Plain text on stdout is also accepted and is treated as developer
  context. To stop the turn entirely, the hook returns a top-level
  `continue: false` plus a `stopReason` string.

The retrieval pipeline, embedder, store, auto-index gates, refresh
throttling, and indexing-banner state are all shared with the
Claude Code hook (lib/hydra_rag_hooks/hook.py). This module is just
the thin envelope adapter on top of the same plumbing.

The Codex-side equivalents of the Claude-side self-installs:

- ~/.codex/config.toml gets a [plugins."hydra-rag-hooks@..."] entry
  on first run (handled by mcp_register.ensure_codex_plugin_registered).
- The plugin manifest at /usr/lib/hydra-rag-hooks/.codex-plugin/
  carries the hook entry, so once `codex plugin add` has been run
  by the user, the hook fires automatically.

Auto-rag mode and the MCP toggle apply to BOTH CLIs since they share
the same toggles.json and the same retrieval index. Toggling auto-rag
on flips the behaviour for Claude AND Codex sessions simultaneously.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import (
    config as config_mod,
    mcp_register,
    migrate,
    paths,
    retrieval,
    runner,
    toggles,
    trigger,
)
from . import hook as claude_hook  # reuse helpers; not the entrypoint
from . import auto_index, progress as progress_mod


def _emit_additional_context(text: str) -> None:
    """Codex's documented shape for injecting context."""
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        },
    }
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _emit_stop(reason: str) -> None:
    """Stop the turn (for the Codex equivalent of Claude Code's
    decision: block status reply, used by bare `rag`)."""
    payload = {"continue": False, "stopReason": reason}
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def run(stdin_text: str, cwd: Path | None = None) -> int:
    try:
        envelope = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        envelope = {"prompt": stdin_text}

    prompt = envelope.get("prompt") or ""
    if not isinstance(prompt, str):
        return 0

    env_cwd = envelope.get("cwd") or envelope.get("working_directory")
    if cwd is None:
        cwd = Path(env_cwd) if env_cwd else Path.cwd()

    if not migrate.env_says_skip():
        try:
            migrate.migrate_index_folder(cwd)
        except Exception:  # noqa: BLE001
            pass

    # Codex equivalent of the ~/.claude.json self-install: keep
    # ~/.codex/config.toml in sync so the MCP server entry follows
    # the user's `crh mcp on/off` toggle.
    try:
        mcp_register.ensure_codex_plugin_registered(disabled=not toggles.mcp_enabled())
    except Exception:  # noqa: BLE001
        pass

    cfg = config_mod.load()
    triggers = config_mod.triggers(cfg)
    lax = bool(cfg.get("lax_trigger", default=False))
    match = trigger.parse(prompt, triggers, lax=lax)

    # Auto-rag mode is shared state with the Claude hook.
    if match is None and toggles.auto_rag_enabled() and claude_hook._eligible_for_auto_rag(prompt):
        match = trigger.TriggerMatch(query=prompt.strip(), tag=None)

    if match is None:
        # Non-RAG turn. Surface still-indexing or completion banners
        # the same way the Claude hook does, but use the Codex
        # additionalContext envelope.
        banner = _maybe_indexing_banner_text(cwd)
        if banner:
            _emit_additional_context(banner)
        return 0

    if match.command == "status":
        return _emit_status_codex(cwd)

    # Tagged retrieval bypasses auto-index.
    if match.tag is not None:
        indexes = retrieval.resolve_indexes(cwd, match.tag)
        if not indexes:
            scope_desc = "any registered store" if match.tag == "all" else f"tag '{match.tag}'"
            print(f"hydra-rag-hooks: no index found for {scope_desc}.",
                  file=sys.stderr, flush=True)
            return 0
        return _emit_retrieval_codex(match.query, indexes, cfg)

    existing = paths.find_index(cwd)
    if existing is not None and claude_hook._index_is_populated(existing):
        scope = existing.parent
        try:
            runner.maybe_refresh(scope)
        except Exception as e:  # noqa: BLE001
            print(f"hydra-rag-hooks: background refresh skipped ({e}).",
                  file=sys.stderr, flush=True)
        return _emit_retrieval_codex(match.query, [existing], cfg)

    if existing is not None:
        scope_for_error = existing.parent
        last = progress_mod.read(existing)
        if last.state == "error":
            print(
                f"hydra-rag-hooks: previous indexing of {scope_for_error} "
                f"failed: {last.message}",
                file=sys.stderr, flush=True,
            )
            return 0

    # No index. Decide auto-index.
    decision = auto_index.decide(cwd)
    if not decision.allow:
        print(f"hydra-rag-hooks: {decision.reason}", file=sys.stderr, flush=True)
        return 0

    scope = decision.scope
    assert scope is not None
    index_dir = scope / paths.INDEX_DIR_NAME
    if progress_mod.is_active(index_dir):
        prog = progress_mod.read(index_dir)
        msg = prog.as_human() or f"hydra-rag-hooks: indexing {scope} in progress."
        print(f"{msg}. Type `rag` to check progress.",
              file=sys.stderr, flush=True)
        return 0

    last = progress_mod.read(index_dir)
    if last.state == "error":
        print(
            f"hydra-rag-hooks: previous indexing attempt of {scope} failed: "
            f"{last.message}",
            file=sys.stderr, flush=True,
        )
        return 0

    runner.fork_detach_index(scope, kind="indexing")
    claude_hook._write_queued_query(scope, match.query)
    print(
        f"hydra-rag-hooks: indexing {scope} in the background. "
        f"This is a one-time setup per project. "
        f"Type `rag` (alone) any time to check progress.",
        file=sys.stderr, flush=True,
    )
    _emit_additional_context(
        f"[hydra-rag-hooks] heads-up: the user typed `rag {match.query}` "
        f"but no index exists for this folder yet. Indexing has just "
        f"started in the background; retrieval will be available on the "
        f"next `rag <q>` once it completes. Answer the user's question "
        f"from your training knowledge for this turn, and tell them their "
        f"earlier query will be ready to replay shortly."
    )
    return 0


def _emit_retrieval_codex(query: str, indexes, cfg) -> int:
    """Run retrieval and emit chunks via Codex's additionalContext envelope."""
    top_k = int(cfg.get("top_k", default=5) or 5)
    try:
        hits = retrieval.retrieve(query, indexes, top_k=top_k, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        print(f"hydra-rag-hooks: retrieval error: {e}",
              file=sys.stderr, flush=True)
        return 0
    if not hits:
        print("hydra-rag-hooks: no relevant chunks found in index.",
              file=sys.stderr, flush=True)
        return 0
    text = retrieval.format_context(
        hits,
        header="<context source=\"hydra-rag-hooks\">",
        footer="</context>",
    )
    _emit_additional_context(text)
    return 0


def _emit_status_codex(cwd: Path) -> int:
    """Bare `rag` status. Codex CLI accepts `continue: false` to stop the
    turn without invoking the model, the equivalent of Claude Code's
    `decision: block`."""
    existing = paths.find_index(cwd)
    if existing is None:
        decision = auto_index.decide(cwd)
        scope_desc = str(decision.scope) if decision.scope else str(cwd)
        if not decision.allow:
            _emit_stop(
                f"hydra-rag-hooks: no index for this folder, and auto-index "
                f"refused: {decision.reason}"
            )
            return 0
        scope = decision.scope
        assert scope is not None
        index_dir = scope / paths.INDEX_DIR_NAME
        if not progress_mod.is_active(index_dir):
            runner.fork_detach_index(scope, kind="indexing")
        _emit_stop(
            f"hydra-rag-hooks: indexing {scope_desc} in the background. "
            f"This is a one-time setup. Type `rag` again to check progress."
        )
        return 0

    scope = existing.parent
    prog = progress_mod.read(existing)
    last = progress_mod.read_last_run(existing)
    is_active = progress_mod.is_active(existing)

    if is_active:
        elapsed = claude_hook._human_duration(__import__("time").time() - prog.started_at)
        verb = "indexing" if prog.state == "indexing" else "refreshing"
        counter = (
            f"{prog.files_done}/{prog.files_total} files"
            if prog.files_total > 0
            else f"{prog.files_done} files so far"
        )
        _emit_stop(
            f"hydra-rag-hooks: {verb} {scope}\n  {counter}\n  elapsed {elapsed}"
        )
        return 0

    if prog.state == "error":
        _emit_stop(f"hydra-rag-hooks: last indexing of {scope} failed: {prog.message}")
        return 0

    if last is not None:
        ago = claude_hook._human_duration(__import__("time").time() - last.finished_at)
        files = last.files_indexed or last.files_total
        _emit_stop(
            f"hydra-rag-hooks: index ready for {scope}\n"
            f"  chunks: {last.chunks_added}\n"
            f"  files: {files}\n"
            f"  last {last.kind}: {ago} ago"
        )
        return 0
    _emit_stop(f"hydra-rag-hooks: index ready for {scope} (no run stats).")
    return 0


def _maybe_indexing_banner_text(cwd: Path) -> str | None:
    """Mirror of hook._maybe_emit_indexing_banner, returning text rather
    than emitting it directly so the Codex envelope wrapper can pack it
    into hookSpecificOutput.additionalContext.
    """
    existing = paths.find_index(cwd)
    if existing is None:
        return None
    scope = existing.parent
    if progress_mod.is_active(existing):
        prog = progress_mod.read(existing)
        if prog.state == "indexing":
            elapsed = claude_hook._human_duration(__import__("time").time() - prog.started_at)
            counter = (
                f"{prog.files_done}/{prog.files_total} files"
                if prog.files_total > 0
                else f"{prog.files_done} files so far"
            )
            return (
                f"[hydra-rag-hooks] heads-up: still indexing {scope} in the "
                f"background ({counter}, {elapsed} elapsed). Retrieval via "
                f"`rag <q>` will work as soon as it finishes."
            )
        return None
    queued = claude_hook._read_queued_query(scope)
    if queued and claude_hook._index_is_populated(existing):
        claude_hook._clear_queued_query(scope)
        return (
            f"[hydra-rag-hooks] indexing complete for {scope}. The user's "
            f"earlier query `rag {queued}` is now ready to retrieve; tell "
            f"them to type that again to use the freshly-built index."
        )
    return None


def main(argv: list[str] | None = None) -> int:
    text = sys.stdin.read() if not sys.stdin.isatty() else ""
    return run(text)


if __name__ == "__main__":
    raise SystemExit(main())
