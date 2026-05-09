"""crh rag/mcp toggle subcommands.

Two surfaces toggle here:

  crh rag on|off|toggle|status     auto-rag mode (every prompt becomes a
                                    rag query, no keyword needed)
  crh mcp on|off|toggle|status     enable/disable the MCP server entry in
                                    the user's ~/.claude.json

The bundled `/rag` Claude Code slash command shells out to `crh rag
toggle` so users get one-keystroke control from inside Claude Code
itself. The CLI form exists so users can script the same thing from
a shell, and so the slash command's underlying behaviour is testable
without a Claude Code session.
"""

from __future__ import annotations

import argparse

from .. import mcp_register, toggles


def _print_auto_rag_state(enabled: bool) -> None:
    if enabled:
        print("auto-rag: ON")
        print("  Every prompt you submit in Claude Code is now treated as")
        print("  a `rag <prompt>` query: hydra-rag-hooks retrieves relevant")
        print("  chunks from your project index and prepends them before")
        print("  Claude sees the prompt. You no longer need to type `rag`")
        print("  or `rag:`.")
        print("")
        print("  Slash commands (`/...`) and very short prompts are still")
        print("  passed through untouched.")
        print("")
        print("  Turn off again with: crh rag off  (or `/rag` inside Claude Code)")
    else:
        print("auto-rag: OFF")
        print("  Default behaviour: only prompts that begin with `rag`,")
        print("  `rag:`, or `/rag` trigger retrieval. Other prompts pass")
        print("  through with zero token overhead.")
        print("")
        print("  Turn on with: crh rag on  (or `/rag` inside Claude Code)")


def _print_mcp_state(enabled: bool) -> None:
    if enabled:
        print("mcp: ON")
        print("  The claude-rag MCP server is wired into your")
        print("  ~/.claude.json. Claude Code can call its `rag_search`,")
        print("  `rag_status`, and `rag_list_stores` tools when it judges")
        print("  that retrieval would help and the user did not type the")
        print("  `rag` keyword. Turn off if you don't want Claude to be")
        print("  able to invoke retrieval on its own.")
        print("")
        print("  Turn off with: crh mcp off")
    else:
        print("mcp: OFF")
        print("  The MCP server entry is still present in ~/.claude.json")
        print("  but starts with CLAUDE_RAG_MCP_DISABLED=1, so the process")
        print("  exits immediately and Claude sees no rag_* tools. Only")
        print("  the keyword-triggered hook remains active.")
        print("")
        print("  Turn on with: crh mcp on")


def run_rag(args: argparse.Namespace) -> int:
    action = args.action or "toggle"
    current = toggles.auto_rag_enabled()
    if action == "status":
        _print_auto_rag_state(current)
        return 0
    if action == "on":
        new = True
    elif action == "off":
        new = False
    elif action == "toggle":
        new = not current
    else:
        print(f"crh rag: unknown action: {action}", file=__import__("sys").stderr)
        return 2
    if new != current:
        toggles.set_value("auto_rag", new)
    _print_auto_rag_state(new)
    return 0


def run_mcp(args: argparse.Namespace) -> int:
    action = args.action or "toggle"
    current = toggles.mcp_enabled()
    if action == "status":
        _print_mcp_state(current)
        registered = mcp_register.is_registered()
        print(f"  registered in ~/.claude.json: {registered}")
        return 0
    if action == "on":
        new = True
    elif action == "off":
        new = False
    elif action == "toggle":
        new = not current
    else:
        print(f"crh mcp: unknown action: {action}", file=__import__("sys").stderr)
        return 2
    if new != current:
        toggles.set_value("mcp_enabled", new)
    # Sync ~/.claude.json so the change takes effect on the next Claude
    # Code session without waiting for the next hook invocation.
    try:
        mcp_register.ensure_registered(disabled=not new)
    except Exception as e:  # noqa: BLE001
        print(f"crh mcp: warning: could not sync ~/.claude.json: {e}",
              file=__import__("sys").stderr)
    _print_mcp_state(new)
    return 0
