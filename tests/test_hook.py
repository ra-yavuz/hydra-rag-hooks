"""Smoke tests for the hook entrypoint.

Focus is on the non-blocking paths: status command (now returns
decision:block JSON) and the indexing-banner short-circuit. Anything
that would touch the embedder or LanceDB is out of scope here (covered
by the indexer tests).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra_rag_hooks import hook, paths, progress as progress_mod


def _make_envelope(prompt: str, cwd: Path) -> str:
    return json.dumps({"prompt": prompt, "cwd": str(cwd)})


def _parse_block_envelope(stdout: str) -> dict | None:
    """Status command returns a decision:block JSON envelope on stdout.
    Parse it and return the dict, or None if stdout isn't JSON."""
    stdout = stdout.strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def test_status_no_index_kicks_off_indexing(tmp_path, capsys, monkeypatch):
    # A project with a marker but no index yet. Bare `rag` should
    # block the model AND start indexing in the background.
    (tmp_path / ".git").mkdir()

    started = {}

    def _fake_start(scope):
        started["scope"] = scope

    monkeypatch.setattr(hook, "_start_indexing", _fake_start)

    rc = hook.run(_make_envelope("rag", tmp_path), cwd=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    env = _parse_block_envelope(captured.out)
    assert env is not None, f"expected decision-block JSON, got {captured.out!r}"
    assert env.get("decision") == "block"
    assert "indexing" in env.get("reason", "").lower()
    assert started.get("scope") == tmp_path.resolve()


def test_status_with_populated_index_blocks(tmp_path, capsys):
    # A populated index with last_run.json should report concrete numbers
    # and block the model.
    (tmp_path / ".git").mkdir()
    index_dir = tmp_path / paths.INDEX_DIR_NAME
    index_dir.mkdir()
    (index_dir / "chunks.lance").mkdir()
    progress_mod.write_last_run(
        index_dir,
        progress_mod.LastRun(
            finished_at=0.0,
            elapsed_seconds=12.5,
            kind="indexing",
            files_total=42,
            files_indexed=42,
            files_pruned=0,
            chunks_added=315,
        ),
    )
    rc = hook.run(_make_envelope("rag", tmp_path), cwd=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    env = _parse_block_envelope(captured.out)
    assert env is not None and env.get("decision") == "block"
    reason = env.get("reason", "")
    assert "315" in reason
    assert "ready" in reason
    assert "42" in reason


def test_status_alternate_forms_all_block(tmp_path, capsys, monkeypatch):
    # `rag status`, `/rag`, and bare `rag` should all return decision:block.
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(hook, "_start_indexing", lambda scope: None)

    for form in ("rag", "/rag", "rag status", "rag:"):
        rc = hook.run(_make_envelope(form, tmp_path), cwd=tmp_path)
        assert rc == 0, f"form {form!r} returned {rc}"
        captured = capsys.readouterr()
        env = _parse_block_envelope(captured.out)
        assert env is not None, (
            f"form {form!r} did not produce JSON: {captured.out!r}"
        )
        assert env.get("decision") == "block", (
            f"form {form!r} did not block: {env}"
        )


def test_indexing_banner_on_non_rag_prompt(tmp_path, capsys):
    # An active indexing job for the cwd's tree should produce a banner
    # on stdout for any non-rag prompt.
    import os
    (tmp_path / ".git").mkdir()
    index_dir = tmp_path / paths.INDEX_DIR_NAME
    index_dir.mkdir()
    progress_mod.write(
        index_dir,
        progress_mod.Progress(
            state="indexing",
            started_at=0.0,
            files_done=10,
            files_total=100,
            pid=os.getpid(),  # the test process is, by definition, alive
        ),
    )
    rc = hook.run(_make_envelope("how do I write a regex?", tmp_path), cwd=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert "still indexing" in captured.out
    assert "10/100" in captured.out


def test_no_banner_when_no_active_job(tmp_path, capsys):
    # No progress file -> no banner on a non-rag prompt.
    (tmp_path / ".git").mkdir()
    rc = hook.run(_make_envelope("how do I write a regex?", tmp_path), cwd=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_non_json_input_treated_as_prompt(tmp_path, capsys, monkeypatch):
    # Some Claude Code wrappers may pass raw text. Bare `rag` should
    # still trip the status path and produce a decision:block envelope.
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(hook, "_start_indexing", lambda scope: None)
    rc = hook.run("rag", cwd=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    env = _parse_block_envelope(captured.out)
    assert env is not None and env.get("decision") == "block"


def test_queued_query_persisted_on_first_rag_q(tmp_path, capsys, monkeypatch):
    # `rag <q>` with no index yet: hook should fork-detach indexing
    # AND persist the query for later replay.
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(hook.runner, "fork_detach_index", lambda scope, kind="indexing": None)

    rc = hook.run(_make_envelope("rag where do we handle auth tokens?", tmp_path), cwd=tmp_path)
    assert rc == 0

    # The query should be readable from the cache file.
    queued = hook._read_queued_query(tmp_path.resolve())
    assert queued == "where do we handle auth tokens?"


def test_completion_banner_fires_once(tmp_path, capsys, monkeypatch):
    # After indexing finishes (no active job, populated index, queued
    # query exists), a non-rag prompt should get the completion banner
    # exactly once.
    (tmp_path / ".git").mkdir()
    index_dir = tmp_path / paths.INDEX_DIR_NAME
    index_dir.mkdir()
    (index_dir / "chunks.lance").mkdir()
    hook._write_queued_query(tmp_path.resolve(), "where do we handle auth tokens?")

    # First non-rag prompt: banner fires.
    rc = hook.run(_make_envelope("how do I write a regex?", tmp_path), cwd=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert "indexing complete" in captured.out
    assert "where do we handle auth tokens?" in captured.out

    # Queued file is now cleared.
    assert hook._read_queued_query(tmp_path.resolve()) is None

    # Second non-rag prompt: silent.
    rc = hook.run(_make_envelope("another unrelated question", tmp_path), cwd=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
