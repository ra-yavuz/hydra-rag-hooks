"""CLI smoke tests.

Focus on dispatch, argument parsing, and the pure-filesystem read
paths (status, ls, doctor). Subcommands that touch the embedder
(index/refresh/query) or fork (refresher) need integration tests,
not unit tests; we cover their argument-parsing here only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra_rag_hooks import paths, progress as progress_mod, registry
from hydra_rag_hooks.cli import _build_parser, main
from hydra_rag_hooks.cli import _common, status as status_cmd
from hydra_rag_hooks.cli import doctor as doctor_cmd
from hydra_rag_hooks.cli import tag as tag_cmd


def test_parser_has_known_subcommands():
    parser = _build_parser()
    # Reach into argparse's internals to read the subcommand registry.
    sub_actions = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
    assert sub_actions, "parser has no subparsers action"
    choices = sub_actions[0].choices
    for cmd in ("status", "index", "refresh", "query", "forget", "ls",
                "tag", "untag", "doctor", "auto", "refresher", "rag", "mcp"):
        assert cmd in choices, f"missing subcommand {cmd}"


def test_status_absent(tmp_path, capsys, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    rc = main(["status", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[absent]" in out


def test_status_ready(tmp_path, capsys, monkeypatch):
    (tmp_path / ".git").mkdir()
    index_dir = tmp_path / paths.INDEX_DIR_NAME
    index_dir.mkdir()
    (index_dir / "chunks.lance").mkdir()
    progress_mod.write_last_run(
        index_dir,
        progress_mod.LastRun(
            finished_at=0.0, elapsed_seconds=12.5, kind="indexing",
            files_total=42, files_indexed=42, files_pruned=0, chunks_added=315,
        ),
    )
    monkeypatch.chdir(tmp_path)
    rc = main(["status", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ready]" in out
    assert "315" in out
    assert "42" in out


def test_status_interrupted(tmp_path, capsys, monkeypatch):
    """An indexing job whose pid is dead but .progress claims active.
    Common after an OOM kill or a manual `kill -9`. Status should
    surface 'interrupted' so the user knows to resume."""
    (tmp_path / ".git").mkdir()
    index_dir = tmp_path / paths.INDEX_DIR_NAME
    index_dir.mkdir()
    (index_dir / "chunks.lance").mkdir()
    progress_mod.write(
        index_dir,
        progress_mod.Progress(
            state="indexing", started_at=0.0, files_done=654, files_total=3815,
            pid=999_999_999,  # virtually guaranteed not to exist
            message="progress: 654/3815 files",
        ),
    )
    monkeypatch.chdir(tmp_path)
    rc = main(["status", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[interrupted]" in out
    assert "654/3815" in out
    assert "crh refresh" in out


def test_status_json_shape(tmp_path, capsys, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    rc = main(["status", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == str(tmp_path.resolve())
    assert payload["state"] == "absent"
    assert "log_path" in payload


def test_status_watch_and_json_mutually_exclusive(capsys):
    rc = main(["status", "--watch", "--json"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "mutually exclusive" in err


def test_tag_validates_format():
    err = tag_cmd._validate_tag("UPPER")
    assert err is not None
    err = tag_cmd._validate_tag("ok-tag.1")
    assert err is None
    err = tag_cmd._validate_tag("all")
    assert err is not None and "reserved" in err


def test_doctor_runs_and_returns_int(capsys, monkeypatch):
    # The doctor checks call into real filesystem state; on a CI
    # box without fastembed they'll mostly warn, which is rc=1.
    # We just want to confirm it runs end-to-end without raising.
    rc = main(["doctor", "--json"])
    assert rc in (0, 1, 2)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert all("check" in r and "severity" in r for r in payload)


def test_ls_empty_registry(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    rc = main(["ls"])
    assert rc == 0
    assert "no registered stores" in capsys.readouterr().out


def test_resolve_path_defaults_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _common.resolve_path(None) == tmp_path.resolve()
    assert _common.resolve_path("/some/path") == Path("/some/path").resolve()
