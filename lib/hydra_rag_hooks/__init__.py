"""hydra-rag-hooks: keyword-triggered local RAG hooks for Claude Code AND Codex CLI.

Single shared local index (LanceDB), single embedder, two prompt-submit
hooks (one per supported CLI), one MCP server. Type `rag <q>` in
either CLI; the hook embeds, retrieves, prepends, the model answers.
The index folder is `.hydra-index/`, the same name hydra-llm uses, so
all three tools cooperate on the same store without re-indexing.
"""

__version__ = "0.1.0"
