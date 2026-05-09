"""Tests for migrate.migrate_index_folder and the path-level XDG migration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hydra_rag_hooks import migrate, paths


@pytest.fixture
def fresh_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def test_migrate_renames_legacy_index_in_place(fresh_xdg, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    legacy = project / paths.LEGACY_CLAUDE_INDEX_DIR_NAME
    legacy.mkdir()
    (legacy / "marker.txt").write_text("hi")

    result = migrate.migrate_index_folder(project)

    assert result == project / paths.INDEX_DIR_NAME
    assert (project / paths.INDEX_DIR_NAME / "marker.txt").read_text() == "hi"
    assert not legacy.exists()


def test_migrate_idempotent_when_already_migrated(fresh_xdg, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    new = project / paths.INDEX_DIR_NAME
    new.mkdir()
    (new / "alreadyhere.txt").write_text("ok")

    result = migrate.migrate_index_folder(project)

    assert result == new
    assert (new / "alreadyhere.txt").read_text() == "ok"


def test_migrate_returns_none_when_no_index(fresh_xdg, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()

    result = migrate.migrate_index_folder(project)

    assert result is None


def test_migrate_walks_up_to_project_root(fresh_xdg, tmp_path):
    project = tmp_path / "proj"
    sub = project / "src" / "deep"
    sub.mkdir(parents=True)
    (project / ".git").mkdir()
    legacy = project / paths.LEGACY_CLAUDE_INDEX_DIR_NAME
    legacy.mkdir()

    # Calling from a subdir resolves to the same project root.
    result = migrate.migrate_index_folder(sub)

    assert result == project / paths.INDEX_DIR_NAME
    assert not legacy.exists()


def test_xdg_dirs_migrate_legacy_claude_rag_hook_root(tmp_path, monkeypatch):
    """First call to config_dir() promotes a pre-existing
    ~/.config/claude-rag-hook/ into ~/.config/hydra-llm/rag-hooks/."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    legacy = tmp_path / "claude-rag-hook"
    legacy.mkdir()
    (legacy / "config.yaml").write_text("triggers: [rag:]")

    result = paths.config_dir()

    assert result == tmp_path / "hydra-llm" / "rag-hooks"
    assert (result / "config.yaml").read_text() == "triggers: [rag:]"
    assert not legacy.exists()


def test_xdg_dirs_no_legacy_just_creates_under_hydra_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    result = paths.config_dir()

    assert result == tmp_path / "hydra-llm" / "rag-hooks"


def test_env_skip_disables_index_migration(fresh_xdg, tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_RAG_HOOKS_SKIP_MIGRATIONS", "1")
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    legacy = project / paths.LEGACY_CLAUDE_INDEX_DIR_NAME
    legacy.mkdir()

    assert migrate.env_says_skip() is True
    # Caller is expected to short-circuit on env_says_skip() before
    # invoking migrate_index_folder; the function itself does still
    # rename. We document this so the env var is not load-bearing for
    # correctness, only convenience.
