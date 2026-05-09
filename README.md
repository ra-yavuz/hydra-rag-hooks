# hydra-rag-hooks

> **Type `rag <question>` in Claude Code or Codex CLI. Get a retrieval-augmented answer.**
>
> Keyword-triggered local RAG hooks for both Anthropic's Claude Code and
> OpenAI's Codex CLI. The first `rag` query inside any project folder
> auto-indexes that folder in the background; the next `rag <q>`
> retrieves relevant chunks and prepends them to the prompt before the
> model sees it. Local-first, deterministic, zero token overhead on
> prompts that do not start with the trigger.
>
> One package, two CLIs, one shared index folder. The index also
> doubles as the [hydra-llm](https://ra-yavuz.github.io/hydra-llm/)
> store, so the three tools cooperate by default.

## What you do

```text
sudo apt install hydra-rag-hooks
```

That's it for Claude Code: the package wires itself into
`/etc/claude-code/managed-settings.json` for every user on the machine.
From your next Claude Code session, inside any project folder (one with
a `.git`, `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`,
etc.), type:

```text
> rag where do we handle auth tokens?
```

For Codex CLI, run **once per user**:

```text
codex plugin add /usr/lib/hydra-rag-hooks/codex-plugin
```

Codex has no managed-settings system-wide path, so this per-user opt-in
is the documented way. The same `rag <q>` keyword works in Codex CLI
afterwards, retrieving from the same index that Claude Code uses.

First time in a folder, the hook fork-detaches a background indexer
and your current prompt passes through unchanged. The next `rag <q>`
turn retrieves and prepends. No commands to run, no settings to edit.

To check the index state at any point, type `rag` alone:

```text
> rag
[hydra-rag-hooks status]
scope: /home/you/projects/widgets
state: ready
chunks: 4231
files: 312
last_run: indexing (8m ago, took 47s)
```

## How indexing handles changes

- **First `rag <q>` in a folder:** auto-indexes that folder's project
  root in the background (~30s for a small repo, longer for big ones).
  Your current turn is not blocked; subsequent `rag <q>` turns benefit
  from the index.
- **Subsequent `rag <q>` turns:** if the index is more than 5 minutes
  old, fork-detach an incremental refresh in the background. Only
  changed files re-embed (matched on size + mtime), so a typical
  refresh of a repo where you edited 3 files re-embeds 3 files.
- **Branch switch / mass file changes:** every file's mtime changes
  when git checks it out, so the next refresh re-embeds everything
  that switched. Expected behavior; the current `rag <q>` uses
  whatever's in the index right now while the refresh runs.

The index lives at `<project-root>/.hydra-index/`. Copy a project
folder to another machine and the index moves with it. `git rm -rf
.hydra-index/` to drop it; the next `rag <q>` will rebuild.

## Safety rails (auto-index will NOT run on)

- `$HOME` itself or any direct child of it (`~/.config`, `~/Downloads`)
- `/`, `/etc`, `/var`, `/tmp`, `/usr`, `/opt`, `/root`, `/boot`,
  `/sys`, `/proc`, `/dev`
- Any folder with no project marker (`.git`, `pyproject.toml`,
  `package.json`, `Cargo.toml`, `go.mod`, `Makefile`, etc.) within six
  ancestors. Drop a `.hydra-rag-allow` file in a folder to opt it in.
- Any folder whose walk would touch more than 20,000 files or 500 MB
  of indexable content. Set `HYDRA_RAG_HOOKS_BYPASS_SIZE_CAP=1` to
  override.
- `.claude/`, `.claude-dev/`, and the index folders themselves
  (we never index Claude Code per-project state).

When auto-index is refused, the hook prints a one-line stderr
explanation and your prompt passes through unchanged. The hook never
fails silently and never indexes silently.

## Trigger forms

| Trigger | Effect |
|---|---|
| `rag <text>` | Retrieve from the project root's index. Default form. |
| `rag: <text>` | Same. Colon form is equivalent. |
| `/rag <text>` | Same, slash-command flavour. |
| `rag` (alone) | Print index status. If no index exists yet, kick off indexing. **Ends the turn without invoking the model**. Same for `rag status` and `rag:`. |
| `/rag-toggle` | Toggle auto-rag mode (see below). Equivalent shell command: `crh rag toggle`. |
| `rag@<tag>: <text>` | Federate retrieval across every store carrying `<tag>`. |
| `rag@all: <text>` | Federate across every registered store. |

All trigger forms work identically in Claude Code AND Codex CLI.

## Auto-rag mode (no keyword needed)

Once you've decided "this whole conversation is about my project", you
can flip auto-rag on and skip the keyword entirely:

```text
> /rag-toggle
auto-rag: ON
```

With auto-rag on, every prompt you submit is treated as if you'd typed
`rag <prompt>`: the hook retrieves relevant chunks and prepends them.
Slash commands and very short prompts pass through untouched.

Toggle is shared between CLIs: flipping it inside Claude Code also
turns auto-rag on for your Codex sessions and vice versa, since both
hooks read the same `~/.local/state/hydra-llm/rag-hooks/toggles.json`.

```text
crh rag on        # turn on
crh rag off       # turn off
crh rag toggle    # flip
crh rag status    # show current state
```

## MCP server: model-decided retrieval

In addition to the keyword hooks, hydra-rag-hooks ships a stdio MCP
server (`hydra-rag-mcp`). The model can call its `rag_search` tool
when it judges retrieval would help and the user did not type the
keyword (or the keyword retrieval came back thin and the model wants
a follow-up search).

The MCP server is on by default for Claude Code (auto-registered into
`~/.claude.json` on first hook run) and for Codex CLI (bundled in the
plugin via `.mcp.json`, enabled when the user runs `codex plugin
add`). Toggle off if you don't want the model to be able to retrieve
on its own:

```text
crh mcp off       # disabled
crh mcp on        # back on
crh mcp status    # show current state
```

Toggle is shared between CLIs the same way auto-rag is.

## Operator CLI: `crh`

apt install puts a `crh` binary on `$PATH`. The hook handles
everything inside the CLI; `crh` is for operator-side tasks: watch
indexing progress, run blocking refreshes for scripts, query the
store, manage the auto-refresh daemon, share an index with a
colleague, diagnose the install.

```text
crh status                  # one-liner state of the cwd's index
crh status --watch          # live-redrawing progress display until done
crh status --all            # state of every registered store
crh index [path]            # blocking initial index, with progress bar
crh refresh [path]          # blocking incremental refresh
crh query "retry policy"    # one-shot retrieval to stdout
crh ls                      # list registered stores
crh tag <path> work         # tag for `rag@work: <q>` federation
crh forget <path>           # delete an index, with confirmation
crh doctor                  # diagnose: model cache, embedder, hook wiring

crh rag on|off|toggle|status     # auto-rag mode
crh mcp on|off|toggle|status     # MCP server toggle

crh export [path] [-o out]       # bundle the project's index into a portable archive
crh import <bundle> [path]       # install a bundle from a colleague

crh refresh --rebuild       # drop the existing index and rebuild from scratch
crh index --rebuild         # same on initial-index command
```

`--rebuild` is the migration knob when the embedder model changes
(eg. you switched the configured embedder, or upgraded across a
release that changed the default). It re-embeds every file rather
than skipping unchanged ones.

Auto-refresh daemon (off by default, opt-in):

```text
crh refresher start         # systemctl --user enable --now hydra-rag-hooks-refresher
crh refresher stop
crh refresher status        # systemd state + watched-projects summary
crh auto on [path]          # opt this project into the daemon
crh auto off [path]         # opt out
```

## Sharing an index (`crh export` / `crh import`)

Indexing a large monorepo can take minutes to hours. If a colleague
has already indexed it, they can hand you the result so you do not
have to pay the cost again.

```text
# Sender. From inside the indexed project:
cd ~/regurio-monorepo
crh export
# -> exported /home/alice/regurio-monorepo to
#    ./regurio-monorepo.BAAI-bge-small-en-v1.5.v1.20260509-093850.crh.tar.zst (71.4 MB)

# Receiver. After apt install hydra-rag-hooks, cd into their checkout:
cd ~/regurio-monorepo
crh import ~/Downloads/regurio-monorepo.BAAI-bge-small-en-v1.5.v1.20260509-093850.crh.tar.zst
# -> imported into /home/bob/regurio-monorepo/.hydra-index
#    registered in stores.json; type `rag <question>` to use it.
```

The receiver's **current working directory** is the destination, not
anything baked into the bundle. The bundle records the sender's
project name for display only; the receiver's checkout can be at a
different path or even renamed.

The bundle does not contain your source code. Only embeddings,
chunked text, file manifest, and embedder metadata. That is still
sensitive: if your project contains secrets, the bundle does too.
Treat it like the source.

## Migration from claude-rag-hook v0.6 or older

If you previously ran `claude-rag-hook` v0.6.x:

- **Indexes** are renamed in place: `.claude-rag-index/` becomes
  `.hydra-index/` on the next hook run. Embeddings and chunks
  survive byte-for-byte; LanceDB recognises the table at the new
  path on next open. No re-indexing.
- **Per-user state** at `~/.config/claude-rag-hook/`,
  `~/.cache/claude-rag-hook/`, `~/.local/state/claude-rag-hook/` is
  moved into the unified family location at
  `~/.config/hydra-llm/rag-hooks/` (and the equivalent under cache
  and state). One-shot rename on first run; existing toggles,
  queued queries, registered stores all preserved.
- **System-wide model cache** at `/var/cache/claude-rag-hook/models/`
  is read fallback-style until you reinstall, at which point the
  postinst copies the model files over to the new shared location at
  `/var/cache/hydra-llm/models/`.
- **Hook wiring** in `/etc/claude-code/managed-settings.json` is
  swapped from `claude-rag-hook-hook` to `hydra-rag-hooks-claude-hook`
  by the new postinst.
- **Slash command** at `~/.claude/commands/rag-toggle.md` continues
  to work. The shipped marker is preserved for upgrade-friendly
  edits.

To install: `apt install hydra-rag-hooks`. The transitional
`claude-rag-hook` v0.7.0 package depends on `hydra-rag-hooks`, so
`apt update` will pull this in for you; you can `apt remove
claude-rag-hook` afterwards.

## Configuration (optional)

`~/.config/hydra-llm/rag-hooks/config.yaml`. Defaults are inlined; a
missing file is not an error. Override only what you need:

```yaml
triggers: ["rag:", "/rag"]
lax_trigger: true                 # accept "rag <q>" without the colon
top_k: 5
retrieval:
  timeout_seconds: 8
embedder:
  kind: fastembed                 # or: openai-compatible, hydra-llm
  model: BAAI/bge-small-en-v1.5   # default; ~33M params, 384 dim
  query_prefix: "Represent this sentence for searching relevant passages: "
  document_prefix: ""
  fastembed_batch_size: 4         # ONNX workspace cap
chunking:
  target_chars: 1500
  overlap_chars: 200
walker:
  max_file_size_mb: 1
  respect_gitignore: true
notifications:
  on_index_complete: true
```

## What the apt install actually does

- Installs the hook binaries at `/usr/lib/hydra-rag-hooks/`:
  - `hydra-rag-hooks-claude-hook` (Claude Code invokes via
    managed-settings)
  - `hydra-rag-hooks-codex-hook` (Codex CLI invokes via the plugin)
  - `hydra-rag-mcp` (MCP server, used by both CLIs)
  - Plus `hydra-rag-hooks-admin`, `hydra-rag-hooksd` (internal).
- Installs `/usr/bin/crh` on `$PATH`.
- Merges a hook entry into
  `/etc/claude-code/managed-settings.json` (Claude Code, zero-touch).
- Ships the Codex plugin tree at
  `/usr/lib/hydra-rag-hooks/codex-plugin/`. Postinst prints the
  one-line `codex plugin add ...` command for the user to run.
- Creates `/var/cache/hydra-llm/models/` (mode 2775 root:adm, shared
  across the hydra-* family).
- Copies any pre-existing `/var/cache/claude-rag-hook/models/`
  contents into the new shared location.
- Pulls in `python3-yaml`, `python3-numpy`, `python3-pathspec`.
- Does NOT pull `fastembed` / `lancedb` / `pyarrow` (not packaged
  for Debian). The first time you trigger a `rag <q>`, the hook
  tells you about the one-time `pip install --user fastembed lancedb
  pyarrow`.

## Does Claude Code or Codex CLI self-update break the hook?

No. The hook binaries live at `/usr/lib/hydra-rag-hooks/` and are
referenced from system or user config files that the upstream CLIs
do not modify on self-update. Specifically:

- A Claude Code update does not touch
  `/etc/claude-code/managed-settings.json`. The hook entry stays
  wired.
- A Codex CLI update does not touch `~/.codex/config.toml`. The
  plugin entry stays wired.
- The `hydra-rag` MCP server entry in `~/.claude.json` and the
  Codex plugin's `.mcp.json` reference are also untouched.
- The hook re-registers itself idempotently on every prompt-submit
  as a belt-and-braces recovery, so even a manually-broken state
  is repaired by the next `rag <q>`.

The reverse is also true: an `apt upgrade hydra-rag-hooks` does not
touch any of the upstream CLIs' state.

## Pairs with hydra-llm

[hydra-llm](https://ra-yavuz.github.io/hydra-llm/) is the sibling
project for running local LLMs with RAG built in. The unified
`.hydra-index/` folder name means a folder indexed by either tool is
visible to the other without re-indexing. Two opt-in deeper
integrations:

- **Embedder reuse:** set `embedder.kind: hydra-llm` and
  `embedder.hydra_id: <id>` in the config. The hook resolves the
  embedder via `hydra-llm rag info <id>` and calls its
  `/v1/embeddings`. You stop pulling fastembed via pip and reuse
  whatever embedder you already run for hydra-llm. Saves ~80 MB of
  duplicated ONNX cache and one extra process.
- **Read existing hydra indexes:** if a folder already has a
  `.hydra-index/` from prior hydra-llm use, the hook reads it
  rather than asking you to re-index.

Both integrations are optional. Standalone use is the default.

## Disclaimer / no warranty

Provided **as is, without warranty of any kind**. By installing or
running this software you accept that:

- You alone are responsible for any damage to your hardware, data,
  network, or system.
- The author is **not liable** for any harm, data loss, or other
  damages, however caused.
- This tool is specifically designed to send local content (retrieved
  chunks) to a third-party LLM (Anthropic for Claude Code, OpenAI for
  Codex CLI). If a directory the hook indexes contains secrets,
  credentials, or sensitive personal data, those will be embedded
  into a local LanceDB index and can be retrieved.
- The bundled MCP server lets the model trigger retrieval on its own.
  With the MCP server on (default), the model can decide to read
  indexed content even on prompts where you did not type `rag`.
  Turn it off with `crh mcp off` if you only want retrieval to fire
  when you explicitly ask.
- Auto-rag mode (off by default; toggle with `/rag-toggle` or
  `crh rag on`) treats every prompt as if you'd typed `rag <prompt>`.
  Turn off when you switch contexts so unrelated questions don't
  pull project content into your prompt.
- The hook merges an entry into
  `/etc/claude-code/managed-settings.json` for every user on the
  machine. The Codex plugin entry, by contrast, is per-user opt-in.
- LLM outputs are unreliable. RAG reduces hallucination but does not
  eliminate it.

If you do not accept these terms, do not install or run this
software.

License: [MIT](LICENSE).

## Source

- Code: [github.com/ra-yavuz/hydra-rag-hooks](https://github.com/ra-yavuz/hydra-rag-hooks)
- Project page: [ra-yavuz.github.io/hydra-rag-hooks](https://ra-yavuz.github.io/hydra-rag-hooks/)
- Other ra-yavuz projects: [ra-yavuz.github.io](https://ra-yavuz.github.io/)
