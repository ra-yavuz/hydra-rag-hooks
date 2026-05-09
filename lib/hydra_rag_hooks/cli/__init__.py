"""crh CLI dispatcher.

Operator-facing companion to the UserPromptSubmit hook. The hook is
the integration point with Claude Code; crh is for everything you
want to do from a shell - watch indexing progress, kick off
refreshes, query the store, manage tags, run the auto-refresher
daemon, diagnose problems.

Subcommand surface (full list, each implemented in its own module):

    crh status [--all] [--watch] [--json]   # see what's going on
    crh index [path] [--watch] [--json]     # blocking initial index
    crh refresh [path] [--watch] [--json]   # blocking incremental refresh
    crh query <text> [--top-k N] [--scope path] [--json]
    crh forget [path] [--yes]               # delete an index, with confirmation
    crh ls [--json]                         # list registered stores
    crh tag <path> <tag>                    # add tag for federated retrieval
    crh untag <path> <tag>                  # remove tag
    crh doctor [--json]                     # diagnose the install
    crh auto on [path]                      # opt project into auto-refresh
    crh auto off [path]                     # opt project out
    crh refresher run                       # foreground daemon entrypoint
    crh refresher start                     # systemctl --user wrapper
    crh refresher stop
    crh refresher status [--json]

All commands return 0 on success and non-zero on failure. Errors go
to stderr; structured output (when --json is set) goes to stdout.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from . import (
    auto as auto_cmd,
    doctor as doctor_cmd,
    forget as forget_cmd,
    index as index_cmd,
    ls as ls_cmd,
    query as query_cmd,
    refresher as refresher_cmd,
    share as share_cmd,
    status as status_cmd,
    tag as tag_cmd,
    toggle as toggle_cmd,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crh",
        description=(
            "hydra-rag-hooks command-line tool. The hook fires inside "
            "Claude Code on `rag <q>`; crh is the operator-facing "
            "companion for everything else."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "DISCLAIMER: provided AS IS, no warranty. The hook reads files "
            "in any folder it indexes and ships retrieved chunks to "
            "Anthropic when retrieval triggers. Audit what you index. "
            "See /usr/share/doc/hydra-rag-hooks/README.md."
        ),
    )
    sub = p.add_subparsers(dest="cmd", metavar="<command>")
    sub.required = True

    # status
    s = sub.add_parser(
        "status",
        help="Show index state for cwd (or every registered store with --all).",
        description="Read .progress and .last_run.json; print one-liner. With --watch, redraw until the indexer finishes.",
    )
    s.add_argument("path", nargs="?", default=None, help="Path to inspect (default: cwd).")
    s.add_argument("--all", action="store_true", help="Show every registered store.")
    s.add_argument("--watch", action="store_true", help="Redraw live until indexing finishes or Ctrl-C.")
    s.add_argument("--json", action="store_true", help="Emit JSON instead of human text.")
    s.set_defaults(func=status_cmd.run)

    # index
    i = sub.add_parser(
        "index",
        help="Build the initial index for a folder, blocking with live progress.",
        description="Block the shell while indexing; show live progress bar and ETA.",
    )
    i.add_argument("path", nargs="?", default=None, help="Path to index (default: cwd).")
    i.add_argument("--no-watch", action="store_true", help="Run quietly; no progress bar.")
    i.add_argument("--rebuild", action="store_true",
                   help="Drop any existing index and rebuild from scratch. "
                        "Use this to migrate from a previous embedder.")
    i.add_argument("--json", action="store_true", help="Final summary as JSON instead of human text.")
    i.set_defaults(func=index_cmd.run_index)

    # refresh
    r = sub.add_parser(
        "refresh",
        help="Incremental refresh of an existing index, blocking with live progress.",
        description="Re-embed only changed files. Same UX as `index` but skips unchanged.",
    )
    r.add_argument("path", nargs="?", default=None, help="Path to refresh (default: cwd).")
    r.add_argument("--no-watch", action="store_true", help="Run quietly; no progress bar.")
    r.add_argument("--rebuild", action="store_true",
                   help="Drop the existing index and rebuild from scratch. "
                        "Use this to migrate from a previous embedder.")
    r.add_argument("--json", action="store_true", help="Final summary as JSON.")
    r.set_defaults(func=index_cmd.run_refresh)

    # query
    q = sub.add_parser(
        "query",
        help="One-shot retrieval to stdout (the same chunks the hook would inject).",
        description="Useful for piping to grep, scripts, or comparing what RAG would surface.",
    )
    q.add_argument("text", help="Query text.")
    q.add_argument("--top-k", "-k", type=int, default=5, help="Number of chunks (default: 5).")
    q.add_argument("--scope", default=None, help="Scope path (default: walk up from cwd).")
    q.add_argument("--json", action="store_true", help="Emit JSON list of hits.")
    q.set_defaults(func=query_cmd.run)

    # forget
    f = sub.add_parser(
        "forget",
        help="Delete the index for a folder.",
        description="Removes the .claude-rag-index/ directory and registry entry. Asks before destroying.",
    )
    f.add_argument("path", nargs="?", default=None, help="Path whose index to forget (default: cwd).")
    f.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt.")
    f.set_defaults(func=forget_cmd.run)

    # ls
    ll = sub.add_parser("ls", help="List registered stores (paths, tags, file/chunk counts).")
    ll.add_argument("--json", action="store_true", help="Emit JSON list.")
    ll.set_defaults(func=ls_cmd.run)

    # tag / untag
    t = sub.add_parser("tag", help="Add a tag to a store (for `rag@<tag>: <q>` federated retrieval).")
    t.add_argument("path", help="Path of the store to tag.")
    t.add_argument("tag", help="Tag to add. Lowercase letters, digits, dot/underscore/dash.")
    t.set_defaults(func=tag_cmd.run_tag)

    u = sub.add_parser("untag", help="Remove a tag from a store.")
    u.add_argument("path", help="Path of the store to untag.")
    u.add_argument("tag", help="Tag to remove.")
    u.set_defaults(func=tag_cmd.run_untag)

    # doctor
    d = sub.add_parser(
        "doctor",
        help="Diagnose the install: model cache, embedder, hook wiring, orphan processes.",
    )
    d.add_argument("--json", action="store_true", help="Emit JSON diagnostics.")
    d.set_defaults(func=doctor_cmd.run)

    # auto on / auto off
    a = sub.add_parser(
        "auto",
        help="Enable or disable auto-refresh (file-watcher daemon) for a project.",
    )
    asub = a.add_subparsers(dest="auto_cmd", metavar="<on|off>")
    asub.required = True
    aon = asub.add_parser("on", help="Drop the .auto-refresh marker so the daemon watches this project.")
    aon.add_argument("path", nargs="?", default=None)
    aon.set_defaults(func=auto_cmd.run_on)
    aoff = asub.add_parser("off", help="Remove the marker so the daemon stops watching.")
    aoff.add_argument("path", nargs="?", default=None)
    aoff.set_defaults(func=auto_cmd.run_off)

    # refresher
    rf = sub.add_parser(
        "refresher",
        help="Manage the auto-refresh daemon (systemd user unit; off by default).",
    )
    rfsub = rf.add_subparsers(dest="refresher_cmd", metavar="<run|start|stop|status>")
    rfsub.required = True
    rfsub.add_parser("run", help="Foreground daemon process. Used by the systemd unit.").set_defaults(
        func=refresher_cmd.run_run
    )
    rfsub.add_parser("start", help="systemctl --user start hydra-rag-hooks-refresher.").set_defaults(
        func=refresher_cmd.run_start
    )
    rfsub.add_parser("stop", help="systemctl --user stop hydra-rag-hooks-refresher.").set_defaults(
        func=refresher_cmd.run_stop
    )
    rfst = rfsub.add_parser("status", help="systemctl --user status + watched-projects summary.")
    rfst.add_argument("--json", action="store_true")
    rfst.set_defaults(func=refresher_cmd.run_status)

    # export / import (share an index with a colleague)
    ex = sub.add_parser(
        "export",
        help="Bundle the cwd's .claude-rag-index/ into a portable archive.",
        description=(
            "Pack the cwd's .claude-rag-index/ (LanceDB table, files manifest, "
            "embedder meta) into a single archive a colleague can install with "
            "`crh import`. Default output is the current directory; pass "
            "`--output` to choose a different file or directory. Uses "
            "tar+zstd if available (smaller files); falls back to tar+gzip."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ex.add_argument("path", nargs="?", default=None,
                    help="Project folder whose index to export (default: cwd).")
    ex.add_argument("--output", "-o", default=None,
                    help="Output file or directory. Default: cwd, auto-named "
                         "<project>.<embedder>.v<schema>.<timestamp>.crh.tar.zst")
    ex.add_argument("--force", "-f", action="store_true",
                    help="Overwrite output if it already exists.")
    ex.set_defaults(func=share_cmd.run_export)

    im = sub.add_parser(
        "import",
        help="Install a `crh export` bundle into a project folder.",
        description=(
            "Unpack a bundle produced by `crh export` into the cwd's "
            ".claude-rag-index/. Refuses to overwrite an existing populated "
            "index without `--force`. After unpacking, registers the store "
            "in stores.json so `crh ls` and tag-federated queries see it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    im.add_argument("bundle", help="Path to the .crh.tar.zst (or .tar.gz) file.")
    im.add_argument("path", nargs="?", default=None,
                    help="Project folder to install into (default: cwd).")
    im.add_argument("--force", "-f", action="store_true",
                    help="Overwrite an existing .claude-rag-index/ in the target.")
    im.set_defaults(func=share_cmd.run_import)

    # rag (auto-rag toggle)
    rg = sub.add_parser(
        "rag",
        help="Toggle auto-rag mode (every prompt becomes a `rag` query, no keyword needed).",
        description=(
            "When auto-rag is on, hydra-rag-hooks treats every prompt you "
            "submit in Claude Code as if you had typed `rag <prompt>`: it "
            "retrieves chunks from the project index and prepends them "
            "before Claude sees the prompt. Slash commands and very short "
            "prompts are still passed through untouched.\n\n"
            "Inside Claude Code itself, `/rag` does the same toggle."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    rg.add_argument(
        "action", nargs="?", default="toggle",
        choices=["on", "off", "toggle", "status"],
        help="Action (default: toggle).",
    )
    rg.set_defaults(func=toggle_cmd.run_rag)

    # mcp (MCP server toggle)
    mc = sub.add_parser(
        "mcp",
        help="Toggle the claude-rag MCP server (model-decided retrieval inside Claude Code).",
        description=(
            "When mcp is on, the claude-rag MCP server is registered in "
            "the user's ~/.claude.json and Claude Code can call "
            "`rag_search`, `rag_status`, and `rag_list_stores` when it "
            "judges that retrieval would help. Default on; turn off if "
            "you do not want Claude to be able to retrieve from the "
            "index on its own (only the keyword hook would remain)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mc.add_argument(
        "action", nargs="?", default="toggle",
        choices=["on", "off", "toggle", "status"],
        help="Action (default: toggle).",
    )
    mc.set_defaults(func=toggle_cmd.run_mcp)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        # Clean Ctrl-C from blocking subcommands. Don't print a Python
        # traceback; just exit with a sensible code.
        print("interrupted", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"crh: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
