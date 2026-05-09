"""Configuration loader.

Single YAML file at ~/.config/hydra-rag-hooks/config.yaml. Defaults are
inlined here so a missing file is not an error.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import paths


DEFAULTS: dict[str, Any] = {
    "triggers": ["rag:", "/rag"],
    # Lax form means "rag <text>" (no colon) also triggers. Catches false
    # positives in theory ("rag dolls are weird") but in coding contexts
    # the friction-cost of remembering the colon dwarfs the false-trigger
    # cost. Default on; users can disable in config if they hit a false
    # positive that matters.
    "lax_trigger": True,
    "top_k": 5,
    "embedder": {
        "kind": "fastembed",
        # Default model as of v0.5.0: BAAI/bge-small-en-v1.5.
        # Why this default:
        #   - 33M params (~4x smaller than nomic-embed-text-v1.5).
        #   - 384-dim vectors (~2x smaller index on disk).
        #   - Inference RSS roughly an order of magnitude lower, which
        #     matters on 8-16 GB laptops where the previous nomic
        #     default could OOM during big-repo indexing.
        #   - MTEB retrieval scores within ~1 point of nomic on
        #     English benchmarks; quality difference is negligible
        #     for code search.
        # Power users on big-RAM hosts who want nomic's longer 8192-
        # token context or top-of-leaderboard quality can override
        # in `~/.config/hydra-rag-hooks/config.yaml`:
        #   embedder:
        #     model: nomic-ai/nomic-embed-text-v1.5
        #     query_prefix: "search_query: "
        #     document_prefix: "search_document: "
        # Migration: indexes built with a different embedder are
        # incompatible (different dim, different vocab). The hook
        # detects this on retrieval and asks for `crh refresh
        # --rebuild`.
        "model": "BAAI/bge-small-en-v1.5",
        # BGE prefixes: query gets the BGE retrieval instruction,
        # documents are encoded without any prefix per BGE upstream
        # guidance (model card explicitly says no prefix on
        # passages).
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "document_prefix": "",
        "base_url": "http://127.0.0.1:19080",
        "hydra_id": "nomic-embed-text",
        # ONNX runtime allocates per-call workspace sized for the
        # batch fastembed feeds it. Default fastembed batch_size is
        # 256 which can ratchet RSS to 12+ GB during big indexing
        # runs. Capping at 4 keeps peak ~1.6 GB at no measurable
        # throughput cost. Power users on big-RAM hosts can crank
        # this back up.
        "fastembed_batch_size": 4,
    },
    "chunking": {
        "target_chars": 1500,
        "overlap_chars": 200,
    },
    "walker": {
        "max_file_size_mb": 1,
        "respect_gitignore": True,
    },
    "daemon": {
        "idle_ttl_seconds": 1800,
        "enabled": True,
    },
    "context": {
        "header": "<context>",
        "footer": "</context>",
        "show_source_lines": True,
    },
    "retrieval": {
        # Hard wall-clock cap on the synchronous retrieval path. If the
        # embedder / vector store takes longer than this (cold-start
        # fastembed, slow disk, hung daemon), the hook gives up and lets
        # Claude answer without retrieved context. The user sees a stderr
        # note telling them the index is fine, just slow this turn.
        "timeout_seconds": 8,
    },
    "notifications": {
        # Desktop notification (notify-send) when initial indexing of a
        # new project finishes. Refreshes stay quiet. Off automatically
        # if notify-send isn't on PATH.
        "on_index_complete": True,
    },
}


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def set(self, dotted_key: str, value: Any) -> None:
        parts = dotted_key.split(".")
        cur = self.data
        for k in parts[:-1]:
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
        cur[parts[-1]] = value

    def save(self, path: Path | None = None) -> None:
        path = path or paths.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(self.data, f, sort_keys=False, default_flow_style=False)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path | None = None) -> Config:
    path = path or paths.config_file()
    if not path.exists():
        return Config()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a YAML mapping at the top level")
    return Config(data=_deep_merge(DEFAULTS, data))


def triggers(cfg: Config) -> list[str]:
    triggers = list(cfg.get("triggers", default=DEFAULTS["triggers"]) or [])
    if cfg.get("lax_trigger", default=False):
        triggers.append("rag ")
    return triggers
