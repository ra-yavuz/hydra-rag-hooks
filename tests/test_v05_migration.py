"""Tests for v0.5.0's default-embedder switch and migration path.

Covers:
- The new default embedder is BAAI/bge-small-en-v1.5 with empty
  document prefix and the BGE retrieval query prefix.
- crh status surfaces an embedder-mismatch hint when an existing
  index was built with a different model.
- crh refresh / crh index --rebuild flag drives full_rebuild.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra_rag_hooks import config as config_mod, paths, store
from hydra_rag_hooks.cli import _build_parser, status as status_cmd


def test_default_embedder_is_bge_small():
    cfg = config_mod.Config()
    assert cfg.get("embedder", "model") == "BAAI/bge-small-en-v1.5"
    assert cfg.get("embedder", "kind") == "fastembed"


def test_default_query_prefix_is_bge_retrieval_instruction():
    cfg = config_mod.Config()
    qp = cfg.get("embedder", "query_prefix")
    assert qp == "Represent this sentence for searching relevant passages: ", (
        f"unexpected query_prefix: {qp!r}"
    )


def test_default_document_prefix_is_empty():
    # BGE upstream guidance: "no instruction needed on passages".
    cfg = config_mod.Config()
    assert cfg.get("embedder", "document_prefix") == ""


def test_embedder_hint_no_meta(tmp_path):
    """No meta.yaml means no recorded embedder; mismatch should be False
    (we have nothing to compare). Cheap check that the function tolerates
    missing files."""
    info = status_cmd._embedder_hint(tmp_path)
    assert info["mismatch"] is False
    assert info["recorded_model"] is None


def test_embedder_hint_matching_meta(tmp_path):
    """meta.yaml records the same embedder the user has configured ->
    no mismatch."""
    store.write_meta(tmp_path, "fastembed", "BAAI/bge-small-en-v1.5", 384)
    info = status_cmd._embedder_hint(tmp_path)
    assert info["recorded_kind"] == "fastembed"
    assert info["recorded_model"] == "BAAI/bge-small-en-v1.5"
    assert info["recorded_dim"] == 384
    assert info["mismatch"] is False


def test_embedder_hint_mismatched_meta(tmp_path):
    """An index built with nomic, but the active config now defaults
    to bge-small. The hint surfaces mismatch=True so `crh status`
    can tell the user about `crh refresh --rebuild`."""
    store.write_meta(tmp_path, "fastembed", "nomic-ai/nomic-embed-text-v1.5", 768)
    info = status_cmd._embedder_hint(tmp_path)
    assert info["recorded_model"] == "nomic-ai/nomic-embed-text-v1.5"
    assert info["configured_model"] == "BAAI/bge-small-en-v1.5"
    assert info["mismatch"] is True


def test_status_human_includes_mismatch_hint(tmp_path, capsys, monkeypatch):
    """End-to-end: an index with the old nomic embedder produces a
    `crh status` block that explicitly tells the user how to migrate."""
    (tmp_path / ".git").mkdir()
    index_dir = tmp_path / paths.INDEX_DIR_NAME
    index_dir.mkdir()
    (index_dir / "chunks.lance").mkdir()
    store.write_meta(index_dir, "fastembed", "nomic-ai/nomic-embed-text-v1.5", 768)

    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    rc = status_cmd.run(parser.parse_args(["status", str(tmp_path)]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "embedder:" in out
    assert "nomic" in out
    assert "bge-small" in out
    assert "crh refresh --rebuild" in out


def test_index_subparser_has_rebuild_flag():
    parser = _build_parser()
    args = parser.parse_args(["index", "--rebuild", "/tmp/x"])
    assert args.rebuild is True
    assert args.path == "/tmp/x"


def test_refresh_subparser_has_rebuild_flag():
    parser = _build_parser()
    args = parser.parse_args(["refresh", "--rebuild"])
    assert args.rebuild is True


def test_rebuild_default_is_false():
    parser = _build_parser()
    args = parser.parse_args(["index"])
    assert args.rebuild is False


def test_status_json_includes_embedder_block(tmp_path, capsys, monkeypatch):
    (tmp_path / ".git").mkdir()
    index_dir = tmp_path / paths.INDEX_DIR_NAME
    index_dir.mkdir()
    (index_dir / "chunks.lance").mkdir()
    store.write_meta(index_dir, "fastembed", "nomic-ai/nomic-embed-text-v1.5", 768)

    monkeypatch.chdir(tmp_path)
    parser = _build_parser()
    rc = status_cmd.run(parser.parse_args(["status", "--json", str(tmp_path)]))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "embedder" in payload
    assert payload["embedder"]["mismatch"] is True
    assert payload["embedder"]["recorded_model"] == "nomic-ai/nomic-embed-text-v1.5"
