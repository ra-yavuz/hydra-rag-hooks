"""Embedder backends.

Each backend exposes:

    class Embedder:
        kind: str
        model: str
        dim: int
        def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
        def embed_query(self, text: str) -> list[float]: ...

Pick one with `resolve(cfg)`.
"""

from __future__ import annotations

from typing import Any, Protocol


class Embedder(Protocol):
    kind: str
    model: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def resolve(embedder_cfg: dict[str, Any]) -> Embedder:
    kind = (embedder_cfg.get("kind") or "fastembed").lower()
    if kind == "fastembed":
        from .fastembed_backend import FastEmbedEmbedder
        return FastEmbedEmbedder(
            model=embedder_cfg.get("model") or "nomic-ai/nomic-embed-text-v1.5",
            query_prefix=embedder_cfg.get("query_prefix") or "",
            document_prefix=embedder_cfg.get("document_prefix") or "",
            fastembed_batch_size=embedder_cfg.get("fastembed_batch_size"),
        )
    if kind in {"openai-compatible", "http"}:
        from .http_backend import HttpEmbedder
        return HttpEmbedder(
            base_url=embedder_cfg.get("base_url") or "http://127.0.0.1:19080",
            model=embedder_cfg.get("model") or "embedder",
            query_prefix=embedder_cfg.get("query_prefix") or "",
            document_prefix=embedder_cfg.get("document_prefix") or "",
        )
    if kind == "hydra-llm":
        from .hydra_llm_backend import HydraLLMEmbedder
        return HydraLLMEmbedder(
            embedder_id=embedder_cfg.get("hydra_id") or "nomic-embed-text",
        )
    raise ValueError(f"unknown embedder kind: {kind!r}")
