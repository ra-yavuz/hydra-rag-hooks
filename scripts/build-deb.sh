#!/usr/bin/env bash
# Build a .deb without debhelper.
#
# Layout shipped:
#   /usr/bin/crh                                                (operator-facing CLI)
#   /usr/lib/hydra-rag-hooks/hydra-rag-hooks-claude-hook        (Claude Code invokes)
#   /usr/lib/hydra-rag-hooks/hydra-rag-hooks-codex-hook         (Codex CLI invokes via plugin)
#   /usr/lib/hydra-rag-hooks/hydra-rag-hooks-admin              (postinst/postrm only)
#   /usr/lib/hydra-rag-hooks/hydra-rag-hooksd                   (auto-spawned embedder daemon)
#   /usr/lib/hydra-rag-hooks/hydra-rag-mcp                      (MCP server for both CLIs)
#   /usr/lib/hydra-rag-hooks/hydra_rag_hooks/                   (Python package)
#   /usr/lib/hydra-rag-hooks/codex-plugin/                      (Codex CLI plugin tree, opt-in via `codex plugin add`)
#   /usr/lib/hydra-rag-hooks/commands/rag-toggle.md             (Claude Code slash command source)
#   /usr/lib/systemd/user/hydra-rag-hooks-refresher.service     (auto-refresh daemon, off by default)
#   /usr/share/doc/hydra-rag-hooks/{README.md,DESIGN.md,copyright}
#
# Claude Code: zero-touch. Postinst merges a hook entry into
#   /etc/claude-code/managed-settings.json so every user gets the hook on
#   their next session.
# Codex CLI: per-user opt-in. Postinst prints the one-line `codex plugin
#   add /usr/lib/hydra-rag-hooks/codex-plugin` command. The hook + bundled
#   MCP server share state with the Claude side via the same .hydra-index/
#   folders and the same toggles.json.
# crh CLI: operator-facing - watch indexing, manage tags, run the auto-
#   refresh daemon, share indexes between machines.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(sed -nE '1 s/^[^(]*\(([^)]+)\).*/\1/p' "$ROOT/debian/changelog")
[ -n "$VERSION" ] || { echo "could not parse version from debian/changelog" >&2; exit 1; }
DEB_VERSION="$VERSION"

PKG_DIR="$ROOT/dist/hydra-rag-hooks_${DEB_VERSION}_all"
DEB_OUT="$ROOT/dist/hydra-rag-hooks_${DEB_VERSION}_all.deb"

rm -rf "$PKG_DIR" "$DEB_OUT"
mkdir -p "$PKG_DIR/DEBIAN" \
         "$PKG_DIR/usr/bin" \
         "$PKG_DIR/usr/lib/hydra-rag-hooks/hydra_rag_hooks/embedder" \
         "$PKG_DIR/usr/lib/hydra-rag-hooks/hydra_rag_hooks/cli" \
         "$PKG_DIR/usr/lib/hydra-rag-hooks/commands" \
         "$PKG_DIR/usr/lib/systemd/user" \
         "$PKG_DIR/usr/share/doc/hydra-rag-hooks"

install -m 0755 "$ROOT/bin/hydra-rag-hooks-claude-hook" "$PKG_DIR/usr/lib/hydra-rag-hooks/hydra-rag-hooks-claude-hook"
install -m 0755 "$ROOT/bin/hydra-rag-hooks-codex-hook"  "$PKG_DIR/usr/lib/hydra-rag-hooks/hydra-rag-hooks-codex-hook"
install -m 0755 "$ROOT/bin/hydra-rag-hooks-admin"       "$PKG_DIR/usr/lib/hydra-rag-hooks/hydra-rag-hooks-admin"
install -m 0755 "$ROOT/bin/hydra-rag-hooksd"            "$PKG_DIR/usr/lib/hydra-rag-hooks/hydra-rag-hooksd"
install -m 0755 "$ROOT/bin/hydra-rag-mcp"               "$PKG_DIR/usr/lib/hydra-rag-hooks/hydra-rag-mcp"
install -m 0755 "$ROOT/bin/crh"                         "$PKG_DIR/usr/bin/crh"

# Codex plugin tree shipped under /usr/lib/hydra-rag-hooks/codex-plugin/.
# Users opt in per-machine with `codex plugin add /usr/lib/hydra-rag-hooks/codex-plugin`
# (printed by postinst). The plugin manifest references the codex-hook
# binary above at its absolute path, so the plugin works from the
# system path without copying anything to the user's home.
install -d "$PKG_DIR/usr/lib/hydra-rag-hooks/codex-plugin/.codex-plugin"
install -d "$PKG_DIR/usr/lib/hydra-rag-hooks/codex-plugin/hooks"
install -m 0644 "$ROOT/codex-plugin/plugin.json"        "$PKG_DIR/usr/lib/hydra-rag-hooks/codex-plugin/.codex-plugin/plugin.json"
install -m 0644 "$ROOT/codex-plugin/hooks/hooks.json"   "$PKG_DIR/usr/lib/hydra-rag-hooks/codex-plugin/hooks/hooks.json"
install -m 0644 "$ROOT/codex-plugin/.mcp.json"          "$PKG_DIR/usr/lib/hydra-rag-hooks/codex-plugin/.mcp.json"

# /rag slash command markdown shipped under /usr/lib/hydra-rag-hooks/commands/.
# The hook self-installs a copy into each user's ~/.claude/commands/ on
# first invocation (idempotent; only writes when content differs).
install -m 0644 "$ROOT/commands/rag-toggle.md" "$PKG_DIR/usr/lib/hydra-rag-hooks/commands/rag-toggle.md"

# systemd user unit for the auto-refresh daemon. Off by default;
# users opt in with `crh refresher start` (which is `systemctl --user
# enable --now`). Per-project opt-in is a marker file inside the
# project's index dir; see `crh auto on`.
install -m 0644 "$ROOT/debian/hydra-rag-hooks-refresher.service" \
    "$PKG_DIR/usr/lib/systemd/user/hydra-rag-hooks-refresher.service"

# Copy the package tree, excluding bytecode caches (which accumulate
# stale .pyc files for renamed/removed modules and would ship them).
( cd "$ROOT/lib" && find hydra_rag_hooks -type f -name '*.py' -print0 | \
    xargs -0 -I {} install -D -m 0644 "{}" "$PKG_DIR/usr/lib/hydra-rag-hooks/{}" )

install -m 0644 "$ROOT/README.md"  "$PKG_DIR/usr/share/doc/hydra-rag-hooks/README.md"
install -m 0644 "$ROOT/DESIGN.md"  "$PKG_DIR/usr/share/doc/hydra-rag-hooks/DESIGN.md"
install -m 0644 "$ROOT/LICENSE"    "$PKG_DIR/usr/share/doc/hydra-rag-hooks/copyright"
install -m 0755 "$ROOT/debian/postinst" "$PKG_DIR/DEBIAN/postinst"
install -m 0755 "$ROOT/debian/postrm"   "$PKG_DIR/DEBIAN/postrm"

cat > "$PKG_DIR/DEBIAN/control" <<EOF
Package: hydra-rag-hooks
Version: ${DEB_VERSION}
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-yaml, python3-numpy, python3-pathspec
Recommends: python3-pip
Suggests: hydra-llm
Maintainer: Ramazan Yavuz <yavuzramazan1994@gmail.com>
Homepage: https://ra-yavuz.github.io/hydra-rag-hooks/
Description: keyword-triggered local RAG hooks for Claude Code AND Codex CLI
 Type "rag <question>" inside either Claude Code or OpenAI's Codex CLI;
 the hook embeds the query, retrieves the top relevant chunks from a
 per-folder LanceDB index, and prepends them to the prompt before the
 model sees it. Local-first, deterministic, zero token overhead on
 prompts that do not start with the trigger.
 .
 Two CLIs, one shared index. The same .hydra-index/ folder works for
 Claude Code AND Codex CLI. The folder name is also what the sibling
 hydra-llm project uses, so all three tools cooperate on the same
 store. Existing claude-rag-hook v0.6 indexes (.claude-rag-index/)
 are auto-renamed in place on first run.
 .
 Claude Code install: zero-touch. apt install merges a hook entry into
 /etc/claude-code/managed-settings.json. Every user on the machine
 picks it up on their next Claude Code session.
 Codex CLI install: per-user opt-in. After apt install, run
 "codex plugin add /usr/lib/hydra-rag-hooks/codex-plugin" once per user.
 .
 The fastembed embedder (default) is not packaged for Debian; install
 it via pip if missing: pip install --user fastembed lancedb pyarrow.
 .
 DISCLAIMER: provided AS IS, no warranty. Reads files inside any folder
 it indexes and stores chunked text plus embeddings of those files at
 <folder>/.hydra-index/. Retrieved chunks are sent to the third-party
 LLM provider (Anthropic for Claude Code, OpenAI for Codex CLI) when
 "rag" fires. The author is not liable for any damage. Audit what you
 index. See /usr/share/doc/hydra-rag-hooks/README.md.
EOF

: > "$PKG_DIR/DEBIAN/conffiles"

dpkg-deb --build --root-owner-group "$PKG_DIR" "$DEB_OUT"
echo
echo "Built: $DEB_OUT"
ls -la "$DEB_OUT"
