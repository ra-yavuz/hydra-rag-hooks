"""Tests for paths.models_cache_dir() and the fastembed cache helpers.

We don't import fastembed here (it's an optional runtime dep, not a
test dep), so the actual TextEmbedding constructor isn't exercised.
What we DO test is the cache layout / wipe logic, which is pure stdlib
and decides whether fastembed gets to download cleanly.
"""

from __future__ import annotations

from pathlib import Path

from hydra_rag_hooks import paths
from hydra_rag_hooks.embedder.fastembed_backend import (
    _model_dir_name,
    _onnx_present,
    _wipe_model_dir,
)


def test_models_cache_dir_falls_back_to_user_cache(tmp_path, monkeypatch):
    # Force both system dirs to paths that do not exist; the helper
    # should fall back to ~/.cache/hydra-llm/models/ (family root).
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(paths, "SYSTEM_MODELS_DIR", tmp_path / "no-such-system-dir")
    monkeypatch.setattr(paths, "LEGACY_SYSTEM_MODELS_DIR", tmp_path / "no-such-legacy-system-dir")

    result = paths.models_cache_dir()
    assert result == tmp_path / "hydra-llm" / "models"
    assert result.is_dir()


def test_models_cache_dir_prefers_system_when_writable(tmp_path, monkeypatch):
    fake_system = tmp_path / "fake-system-models"
    fake_system.mkdir()
    monkeypatch.setattr(paths, "SYSTEM_MODELS_DIR", fake_system)
    monkeypatch.setattr(paths, "LEGACY_SYSTEM_MODELS_DIR", tmp_path / "no-such-legacy-system-dir")

    result = paths.models_cache_dir()
    assert result == fake_system


def test_models_cache_dir_falls_back_when_system_unwritable(tmp_path, monkeypatch):
    # New system dir exists but is read-only; legacy system dir does
    # not exist; fall through to user cache under hydra-llm family.
    fake_system = tmp_path / "fake-system-models"
    fake_system.mkdir()
    fake_system.chmod(0o555)
    try:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr(paths, "SYSTEM_MODELS_DIR", fake_system)
        monkeypatch.setattr(paths, "LEGACY_SYSTEM_MODELS_DIR", tmp_path / "no-such-legacy-system-dir")

        result = paths.models_cache_dir()
        assert result == tmp_path / "hydra-llm" / "models"
    finally:
        fake_system.chmod(0o755)


def test_models_cache_dir_uses_legacy_when_only_legacy_exists(tmp_path, monkeypatch):
    # Migration path: pre-rename install left /var/cache/claude-rag-hook/
    # behind. Until the next reinstall does the rename, we keep using
    # those models so the embedder doesn't have to redownload.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(paths, "SYSTEM_MODELS_DIR", tmp_path / "no-such-new-system-dir")
    legacy = tmp_path / "fake-legacy-system-models"
    legacy.mkdir()
    monkeypatch.setattr(paths, "LEGACY_SYSTEM_MODELS_DIR", legacy)

    result = paths.models_cache_dir()
    assert result == legacy


def test_model_dir_name_translates_slashes():
    assert _model_dir_name("nomic-ai/nomic-embed-text-v1.5") == (
        "models--nomic-ai--nomic-embed-text-v1.5"
    )
    assert _model_dir_name("BAAI/bge-small-en") == "models--BAAI--bge-small-en"


def test_onnx_present_returns_true_when_no_cache_at_all(tmp_path):
    # No cached model dir -> True (let fastembed fetch from scratch
    # without us getting in the way).
    assert _onnx_present(tmp_path, "nomic-ai/nomic-embed-text-v1.5") is True


def test_onnx_present_returns_false_when_dir_exists_but_empty(tmp_path):
    # Half-broken cache from a /tmp purge: dir + sub-tree exist, no
    # .onnx file. This is the "wipe and re-download" case.
    model_dir = tmp_path / "models--nomic-ai--nomic-embed-text-v1.5"
    (model_dir / "snapshots" / "abc").mkdir(parents=True)
    (model_dir / "snapshots" / "abc" / "config.json").write_text("{}")
    assert _onnx_present(tmp_path, "nomic-ai/nomic-embed-text-v1.5") is False


def test_onnx_present_returns_true_when_onnx_in_subdir(tmp_path):
    model_dir = tmp_path / "models--nomic-ai--nomic-embed-text-v1.5"
    (model_dir / "snapshots" / "abc").mkdir(parents=True)
    (model_dir / "snapshots" / "abc" / "model.onnx").write_bytes(b"fake-onnx")
    assert _onnx_present(tmp_path, "nomic-ai/nomic-embed-text-v1.5") is True


def test_wipe_model_dir_removes_only_the_model(tmp_path):
    target = tmp_path / "models--nomic-ai--nomic-embed-text-v1.5"
    other = tmp_path / "models--BAAI--bge-small-en"
    target.mkdir()
    (target / "junk").write_text("x")
    other.mkdir()
    (other / "keep").write_text("x")

    _wipe_model_dir(tmp_path, "nomic-ai/nomic-embed-text-v1.5")

    assert not target.exists()
    assert other.exists() and (other / "keep").exists()


def test_wipe_model_dir_is_safe_when_target_missing(tmp_path):
    # Should not raise.
    _wipe_model_dir(tmp_path, "nomic-ai/nomic-embed-text-v1.5")
