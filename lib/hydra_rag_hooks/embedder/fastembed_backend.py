"""Pure-Python embedder via fastembed (ONNX runtime).

Default model: nomic-embed-text-v1.5 (768d, ~80 MB ONNX, no Hugging Face
token needed). Loaded lazily on first call so import time stays cheap.

Cache location: by default fastembed downloads models into
`/tmp/fastembed_cache/`, which is wiped by /tmp cleanup at every
reboot (and intermittently by systemd-tmpfiles during uptime). That
forces a 4-minute re-download on every boot. We override to
`paths.models_cache_dir()` (machine-wide if writable, per-user
otherwise) so the model survives reboots.

Auto-recovery: if a previous download was interrupted or the cache
shell exists but the .onnx file is missing (the /tmp purge case),
fastembed sees the directory and refuses to re-download, throwing
`NoSuchFile`. We detect that case, wipe the broken model dir, and
retry once.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .. import paths


def _model_dir_name(model: str) -> str:
    """fastembed/HuggingFace cache dir name for a given model id.

    "nomic-ai/nomic-embed-text-v1.5" -> "models--nomic-ai--nomic-embed-text-v1.5"
    """
    return "models--" + model.replace("/", "--")


def _onnx_present(cache_root: Path, model: str) -> bool:
    """Cheap heuristic: does the cached model dir contain at least one
    .onnx file? Empty/half-broken caches return False."""
    model_dir = cache_root / _model_dir_name(model)
    if not model_dir.is_dir():
        return True  # nothing cached at all; let fastembed fetch from scratch
    for _ in model_dir.rglob("*.onnx"):
        return True
    return False


def _wipe_model_dir(cache_root: Path, model: str) -> None:
    model_dir = cache_root / _model_dir_name(model)
    if model_dir.is_dir():
        shutil.rmtree(model_dir, ignore_errors=True)


class FastEmbedEmbedder:
    kind = "fastembed"

    # ONNX runtime allocates workspace per inference call sized for the
    # batch fastembed hands it. Default fastembed batch_size is 256
    # which on a 768-dim transformer balloons RSS by ~6GB per batch
    # (~12GB ratchet over a few calls). Capping at 4 keeps per-call
    # peak ~1.6GB at no measurable wall-clock cost (fastembed re-feeds
    # mini-batches sequentially; throughput is bound by ONNX compute,
    # not batch overhead). Power users can crank this back up with
    # `embedder.fastembed_batch_size: 32` etc. in the config.
    DEFAULT_FASTEMBED_BATCH_SIZE = 4

    def __init__(self, model: str, query_prefix: str = "",
                 document_prefix: str = "",
                 fastembed_batch_size: int | None = None):
        self.model = model
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        self.fastembed_batch_size = (
            fastembed_batch_size
            if fastembed_batch_size and fastembed_batch_size > 0
            else self.DEFAULT_FASTEMBED_BATCH_SIZE
        )
        self._model: Any | None = None
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._ensure_loaded()
        assert self._dim is not None
        return self._dim

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            from fastembed import TextEmbedding  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "fastembed is not installed. Install with `pip install fastembed`, "
                "or pick a different embedder.kind in your config "
                "(openai-compatible, hydra-llm)."
            ) from e

        cache_root = paths.models_cache_dir()
        cache_root.mkdir(parents=True, exist_ok=True)

        # Pre-flight: if the model cache shell exists but no .onnx is
        # present (eg. /tmp purge or interrupted download), wipe it so
        # fastembed redownloads cleanly instead of erroring out.
        if not _onnx_present(cache_root, self.model):
            _wipe_model_dir(cache_root, self.model)

        try:
            self._model = TextEmbedding(model_name=self.model, cache_dir=str(cache_root))
        except Exception:
            # One self-heal retry: assume the on-disk cache is broken,
            # wipe the model subdir, and try again with a fresh download.
            _wipe_model_dir(cache_root, self.model)
            self._model = TextEmbedding(model_name=self.model, cache_dir=str(cache_root))

        # Probe dimension with a tiny embedding.
        sample = list(self._model.embed(["probe"]))[0]
        self._dim = int(len(sample))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_loaded()
        prefixed = [f"{self.document_prefix}{t}" for t in texts] if self.document_prefix else texts
        assert self._model is not None
        return [
            list(map(float, v))
            for v in self._model.embed(prefixed, batch_size=self.fastembed_batch_size)
        ]

    def embed_query(self, text: str) -> list[float]:
        self._ensure_loaded()
        prefixed = f"{self.query_prefix}{text}" if self.query_prefix else text
        assert self._model is not None
        return list(map(float, list(self._model.embed([prefixed]))[0]))
