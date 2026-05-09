"""hydra-llm interop embedder.

Reads ~/.config/hydra-llm/embedders.yaml, picks an installed embedder by
id, asks `hydra-llm rag info <id>` for the runtime port, and delegates
to the OpenAI-compatible HttpEmbedder. The hook does not start or stop
the embedder container; the user is responsible for that via
`hydra-llm rag download` and `hydra-llm` usage.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from .http_backend import HttpEmbedder


def _hydra_config_path() -> Path:
    return Path.home() / ".config" / "hydra-llm" / "embedders.yaml"


def _load_catalog() -> list[dict[str, Any]]:
    p = _hydra_config_path()
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if isinstance(data, dict) and isinstance(data.get("embedders"), list):
        return data["embedders"]
    if isinstance(data, list):
        return data
    return []


def _find_entry(embedder_id: str) -> dict[str, Any] | None:
    for e in _load_catalog():
        if isinstance(e, dict) and (e.get("id") == embedder_id or e.get("name") == embedder_id):
            return e
    return None


def _runtime_url(embedder_id: str) -> tuple[str, int] | None:
    """Ask `hydra-llm rag info <id>` for the runtime port. Returns (url, dim) or None."""
    if not shutil.which("hydra-llm"):
        return None
    try:
        out = subprocess.run(
            ["hydra-llm", "rag", "info", embedder_id, "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    base_url = data.get("base_url") or data.get("url")
    dim = int(data.get("dim") or data.get("dimensions") or 0)
    if base_url and dim:
        return base_url, dim
    return None


class HydraLLMEmbedder:
    kind = "hydra-llm"

    def __init__(self, embedder_id: str):
        self.embedder_id = embedder_id
        self.model = embedder_id
        entry = _find_entry(embedder_id)
        if entry is None:
            raise RuntimeError(
                f"hydra-llm embedder {embedder_id!r} not found in "
                f"{_hydra_config_path()}. Run `hydra-llm rag download {embedder_id}`."
            )
        self._entry = entry
        # Defer URL probe until first use; the embedder container may not be running yet.
        self._inner: HttpEmbedder | None = None

    def _ensure_inner(self) -> HttpEmbedder:
        if self._inner is not None:
            return self._inner
        runtime = _runtime_url(self.embedder_id)
        if runtime is None:
            raise RuntimeError(
                f"could not resolve hydra-llm embedder {self.embedder_id!r} runtime. "
                f"Run `hydra-llm rag info {self.embedder_id}` and confirm it is running."
            )
        url, dim = runtime
        prefix_q = self._entry.get("query_prefix") or ""
        prefix_d = self._entry.get("document_prefix") or ""
        emb = HttpEmbedder(
            base_url=url,
            model=self.embedder_id,
            query_prefix=prefix_q,
            document_prefix=prefix_d,
        )
        emb._dim = dim  # noqa: SLF001 (known up front, no need to probe)
        self._inner = emb
        return emb

    @property
    def dim(self) -> int:
        return self._ensure_inner().dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._ensure_inner().embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._ensure_inner().embed_query(text)
