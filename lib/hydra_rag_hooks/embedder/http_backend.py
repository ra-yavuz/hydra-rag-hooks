"""OpenAI-compatible /v1/embeddings client.

For users who already run a local llama-server (or any other OpenAI-compatible
endpoint) in --embeddings mode. The hook does not start or stop the
container; the user is responsible for that.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class HttpEmbedder:
    kind = "http"

    def __init__(self, base_url: str, model: str, query_prefix: str = "", document_prefix: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed_query("probe"))
        return self._dim

    def _post(self, url: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"embedder HTTP error at {url}: {e}") from e

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        url = f"{self.base_url}/v1/embeddings"
        out: list[list[float]] = []
        # Most servers accept a list, but we batch in 64s for safety.
        for i in range(0, len(texts), 64):
            chunk = texts[i : i + 64]
            data = self._post(url, {"input": chunk, "model": self.model})
            embs = data.get("data") or []
            for item in embs:
                v = item.get("embedding") or []
                out.append([float(x) for x in v])
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        prefixed = [f"{self.document_prefix}{t}" for t in texts] if self.document_prefix else texts
        return self._embed(prefixed)

    def embed_query(self, text: str) -> list[float]:
        prefixed = f"{self.query_prefix}{text}" if self.query_prefix else text
        out = self._embed([prefixed])
        if not out:
            raise RuntimeError("embedder returned no vectors")
        return out[0]
