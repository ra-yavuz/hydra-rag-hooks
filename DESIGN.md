# claude-rag-hook: design notes

> **hydra-rag for Claude.**
> Type `rag: <question>` in Claude Code; local retrieval-augmented
> generation runs against an indexed folder before Claude ever sees the
> prompt. Cheap, deterministic, local-first.

Status: design only. Nothing implemented yet. This document describes what the
project will be when it ships, and the design decisions behind that shape.

## What it is

A `UserPromptSubmit` hook for Claude Code that does keyword-triggered local
retrieval-augmented generation. The user types a keyword prefix (default
`rag:`); the hook walks back through their cwd, looks for an index, retrieves
the top-K relevant chunks, and prepends them to the prompt as context. Claude
Code receives an enriched prompt and answers normally.

That is the whole tool. It is deliberately small.

## What it is not

- Not an MCP server. The model never decides when to retrieve. The user does,
  by typing the keyword. If you want a model-decides surface, install
  `shinpr/mcp-local-rag` alongside; the two are complementary.
- Not a chat-model runner. It does not start LLMs, manage GGUFs, or expose
  endpoints. It only does retrieval.
- Not always-on. A prompt that does not start with the trigger keyword passes
  through untouched. Zero token overhead on prompts that do not need RAG.

## Why this specific shape

Three local-first RAG-for-Claude-Code projects already exist on GitHub:
`shinpr/mcp-local-rag`, `ItMeDiaTech/rag-cli`, `zilliztech/claude-context`.
All three are MCP-driven (model decides when to retrieve) or full plugins
(always inject). None of them implement a simple keyword-triggered hook.

The keyword-triggered hook is genuinely different because:

1. It is the cheapest option per-turn. No tool-call round trip; no context
   injection on prompts that do not need RAG; no MCP-tool definitions
   bloating every system prompt.
2. It is deterministic. The user sees the keyword, types it, gets retrieval.
   No "why didn't the model use the tool that time" debugging.
3. It composes with model-decides RAG cleanly. Power users can install both:
   keyword-trigger for the fast known-need lookups; MCP server for the rare
   "Claude noticed I might want to look something up" cases.

The only architectural cost is asking the user to learn the keyword. That
cost is small and the keyword is configurable.

## Trigger forms

The hook recognises these prompt prefixes (case-insensitive, leading
whitespace tolerated):

- `rag: <text>` &mdash; retrieve chunks for `<text>`, prepend to the prompt,
  pass `<text>` itself through as the user's question.
- `rag <text>` &mdash; same, looser form. Optional; configurable on/off.
- `/rag <text>` &mdash; alternative form for users who want a slash-command
  feel.

A prompt that does not match any trigger form falls through to Claude Code
unchanged. The hook is a no-op for non-RAG turns.

The trigger keyword is configurable in the user's `~/.config/claude-rag-hook/
config.yaml` (default: `rag`). Users with workflow-specific keywords (e.g.
`docs:` for a docs-only index) can register additional triggers there.

## Where the index lives

Same per-folder pattern hydra-llm uses: `<folder>/.claude-rag-index/`. The
hook resolves the index by walking up from cwd until it finds one, the way
git finds `.git/`. If no index is found, the hook prints a one-line note
("no .claude-rag-index/ in this folder or any parent; run `claude-rag-hook
index .` to create one") and falls back to passing the prompt through
without retrieval.

A small per-user registry at `~/.local/state/claude-rag-hook/stores.json`
tracks every folder the user has indexed, so a future `claude-rag-hook ls`
or `prune` command can audit them. Not load-bearing; same pattern hydra-llm
already uses.

### Cross-folder retrieval

For users who want retrieval across multiple indexed folders from any cwd,
the trigger form `rag@<tag>: <text>` retrieves from every store tagged with
`<tag>` (federated, RRF-fused). Tags are set at index time:

    claude-rag-hook index ~/projects/foo --tag work
    claude-rag-hook index ~/Documents/notes --tag personal

Then `rag@work: where do we handle auth` searches both work-tagged stores.

Optional, opt-in. The default keyword `rag:` always means "the index in or
above cwd".

## Stack choices

### Embedder

Default: a pure-Python embedder via `fastembed` (or `sentence-transformers`
if `fastembed` proves heavyweight). One default model:
`nomic-embed-text-v1.5` (768d, ~80 MB ONNX, no Hugging Face token needed).
Pure-Python avoids forcing Docker on users who do not run hydra-llm.

Power users can opt into a Docker-llama-server embedder via config (same
runtime hydra-llm uses, with the catalog of bigger Qwen3 / nomic-embed-code
embedders). When that is configured, the hook resolves the embedder
endpoint and calls it via `/v1/embeddings`. The hook does not start or stop
the container; the user is responsible for that. (Or it can be wired to
hydra-llm: see "Optional hydra-llm interop" below.)

The choice is per-index. `meta.yaml` records which embedder was used so a
later query knows what to embed the user's text with.

### Vector store

LanceDB. Single embedded library, file-based, supports HNSW for fast ANN
once a corpus grows past a few thousand chunks, schema evolution built in.
Same choice hydra-llm made for the same reasons.

### File walker, classifier, chunker

Same shape as hydra-llm: pathspec for `.gitignore`, builtin blacklist for
the obvious junk (`node_modules`, `.venv`, lockfiles, binaries, files >1
MB). Line-aware overlap chunker (1500-char target, 200-char overlap, never
splits mid-line). Classifier tags each chunk as code or prose by extension
+ canonical basenames + shebang sniff.

Different from hydra-llm: this tool runs **single-embedder by default**
from day one. No code/prose dual-index split. Hydra learned this lesson the
hard way; we do not need to relearn it. Users who want the dual split can
opt into it via config and the hook will fuse via RRF.

This choice keeps disk footprint and embedder management minimal. One
embedder, one LanceDB table per folder, done.

## Hook protocol

Claude Code's `UserPromptSubmit` hook contract: stdin receives a JSON
envelope with the user's prompt and conversation metadata; stdout becomes
context that Claude sees; exit code 0 means proceed.

Sketch of what the hook does on a triggered prompt:

1. Parse the prompt prefix. Extract the query (text after `rag:` etc.) and
   any tag scope.
2. If no index is reachable from cwd, print a single explanatory line on
   stderr and exit 0. The original prompt is passed to Claude unchanged.
3. Resolve the embedder (per-index `meta.yaml`).
4. Embed the query, search LanceDB, retrieve top-K chunks (default K=5).
5. Format chunks as a `<context>...</context>` block, write to stdout.
   Claude Code appends this to the user's prompt before sending to Claude.
6. Exit 0.

Total latency target: under 200 ms on a warm fastembed instance for
indexes up to ~50k chunks. That sets the keep-the-embedder-in-memory
constraint discussed below.

### Embedder warm-keeping

A cold fastembed init can take 1-2 seconds; we cannot pay that on every
keyword-triggered turn. The hook starts a small persistent daemon
(`claude-rag-hookd`) on first use that keeps the embedder loaded and
listens on a Unix domain socket at `~/.cache/claude-rag-hook/embedder.sock`.
The hook's "embed query" path is just one local-socket round-trip.

The daemon idles out after N minutes of no activity (default 30) and
re-spawns on the next call. It is not a system service; it is a per-user
process that vanishes when not needed. Users who do not want a background
process can disable warm-keeping in config and pay the cold-start cost.

## CLI surface

    claude-rag-hook install              one-time: write the hook entry to
                                          ~/.claude/settings.json
    claude-rag-hook uninstall            remove that entry
    claude-rag-hook index [path]         walk + chunk + embed + store
    claude-rag-hook query "<text>"       sanity-check retrieval (no Claude)
    claude-rag-hook ls                   list indexed folders
    claude-rag-hook rm <path>            drop an index
    claude-rag-hook config <key> [val]   read or set config keys

The hook itself runs as `claude-rag-hook hook` (the entry Claude Code's
settings.json points at). That subcommand reads the hook envelope from
stdin and emits context on stdout.

## Configuration

`~/.config/claude-rag-hook/config.yaml`:

    triggers:
      - "rag:"
      - "/rag"
      - "rag "          # the lax form; off by default
    top_k: 5
    embedder:
      kind: fastembed
      model: nomic-embed-text-v1.5
      # Or for Docker-llama-server users:
      # kind: openai-compatible
      # base_url: http://127.0.0.1:19080
      # query_prefix: "search_query: "
      # document_prefix: "search_document: "
    chunking:
      target_chars: 1500
      overlap_chars: 200
    walker:
      max_file_size_mb: 1
      respect_gitignore: true
    daemon:
      idle_ttl_seconds: 1800

## Optional hydra-llm interop

If hydra-llm is installed on the same machine, claude-rag-hook can reuse
its embedder catalog and indexes. Two integrations:

- **Embedder reuse:** `embedder.kind: hydra-llm` reads
  `~/.config/hydra-llm/embedders.yaml`, picks an installed embedder by id,
  asks `hydra-llm rag info <id>` for the runtime port, and calls
  `/v1/embeddings` directly. The hook does not start the container; the
  user runs `hydra-llm` once, then claude-rag-hook reuses it.
- **Store reuse:** if a folder has a `.hydra-index/` instead of a
  `.claude-rag-index/`, claude-rag-hook recognises the schema and queries
  it. This means hydra-llm users do not have to re-index for Claude Code.

Both are optional. Standalone use is the default. The interop exists so
the two tools cooperate cleanly for users who run both.

## Project skeleton

    ~/github-ra-yavuz/claude-rag-hook/
      DESIGN.md           this file
      README.md           user-facing pitch + install + usage
      LICENSE             MIT
      bin/claude-rag-hook CLI entrypoint
      lib/hydra_rag_hooks/
        __init__.py
        cli.py
        hook.py           the hook subcommand: stdin -> stdout
        daemon.py         the warm embedder daemon
        indexer.py        walk + chunk + embed
        store.py          LanceDB
        embedder/
          fastembed.py    pure-Python path
          http.py         OpenAI-compat /v1/embeddings client
          hydra_llm.py    hydra-llm interop
        config.py
        paths.py
      hooks/
        user-prompt-submit.template.json   what to merge into ~/.claude/settings.json
      debian/             standard packaging
      docs/index.html     project page
      scripts/build-deb.sh

Same scaffolding pattern as hydra-llm and the other ra-yavuz projects.

## Distribution

Same pattern as the other ra-yavuz projects:

- Build deb via `scripts/build-deb.sh`.
- Push to `~/github-ra-yavuz/apt/pool/main/c/claude-rag-hook/`.
- Project page at `docs/index.html`, served on
  `https://ra-yavuz.github.io/claude-rag-hook/`.
- Hub card on the apex page with appropriate badges (`General`, `CLI`,
  `Linux`, `RAG`).
- Profile README bullet under the right category.

Only Python deps are `pyyaml`, `numpy`, `pathspec`, `lancedb`, and one of
`fastembed` or a sentence-transformers stack. All apt-installable on
modern Ubuntu/Debian (`python3-yaml`, `python3-numpy`, `python3-pathspec`),
with pip fallbacks in `install.sh` for the ones that are not packaged.

## Open design questions before implementation

1. **Daemon protocol.** Plain JSON over a Unix domain socket is the
   obvious choice. Stick with that, or use HTTP loopback for easier
   debugging? Vote: Unix socket. Less attack surface, no port allocation.

2. **Should `claude-rag-hook install` modify `~/.claude/settings.json`
   directly, or just print the JSON snippet for the user to paste?**
   Modifying settings.json automatically is more user-friendly but
   touching another tool's config file is a surface for breakage. Vote:
   modify automatically with a `--dry-run` flag and a clear backup. Print
   a clear "I added this hook entry; revert with claude-rag-hook
   uninstall" message.

3. **Multi-folder default scope.** When the user types just `rag: ...`
   from a folder with an index, search only that folder. When they type
   `rag@all: ...`, federate across every registered store. Sane?

4. **Update Claude Code's prompt-injection-detection behaviour.** Per the
   GitHub issue search, `UserPromptSubmit` stdout output triggered some
   versions of Claude Code's prompt-injection detector. Need to verify
   the current Claude Code version handles this cleanly before shipping.
   The injected `<context>...</context>` block must not trip false
   positives.

5. **Privacy: what does the daemon log?** Default: nothing beyond
   start/stop and error tracebacks. The query text and chunks never
   touch disk. Configurable opt-in for debug logging.

## Disclaimer (mandatory per ra-yavuz/CLAUDE.md)

When the project ships, the README, project page, and CLI `--help` output
must carry a no-warranty disclaimer in the same shape as hydra-llm's.
Specific risks for this project:

- The hook reads files inside any folder you index and stores chunked
  text plus embeddings of those files at `<folder>/.claude-rag-index/`.
- The hook injects retrieved chunks into the prompt that is sent to
  Anthropic. **Indexed material is shipped to Claude when retrieval
  triggers.** Audit what you index before triggering retrieval. The
  README's privacy section must spell this out clearly: indexes never
  leave your machine on their own, but retrieval results do, because
  that is the entire point.
- The daemon runs as the user; a misconfigured config file or a malicious
  process able to write to it could be used to exfiltrate retrieval
  results.

The latter point matters more for this tool than for hydra-llm because
this tool is specifically designed to send local content to a third-party
LLM. The disclaimer should be explicit about that.
