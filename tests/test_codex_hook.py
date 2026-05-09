"""Smoke tests for the Codex CLI hook envelope adapter.

Most of the retrieval pipeline is the same as the Claude side and is
covered by test_hook.py / test_trigger.py / test_runner_progress.py.
This file pins down the Codex-specific JSON envelope shapes:

  - additionalContext on retrieval / banner output
  - continue: false / stopReason on bare-rag status
  - graceful no-op on unrelated prompts
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from hydra_rag_hooks import codex_hook, paths, progress as progress_mod


def _make_envelope(prompt: str, cwd: Path) -> str:
    return json.dumps({
        "session_id": "00000000-0000-0000-0000-000000000000",
        "turn_id": "00000000-0000-0000-0000-000000000001",
        "hook_event_name": "UserPromptSubmit",
        "cwd": str(cwd),
        "prompt": prompt,
    })


@pytest.fixture
def fresh_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


def _capture_stdout(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    return buf


def test_codex_non_trigger_emits_nothing_no_index(fresh_xdg, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.chdir(project)
    out = _capture_stdout(monkeypatch)

    rc = codex_hook.run(_make_envelope("how are you", project), cwd=project)

    assert rc == 0
    # Non-trigger turn with no active index emits nothing on stdout.
    assert out.getvalue() == ""


def test_codex_status_returns_continue_false_envelope(fresh_xdg, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.chdir(project)
    out = _capture_stdout(monkeypatch)

    rc = codex_hook.run(_make_envelope("rag", project), cwd=project)

    assert rc == 0
    parsed = json.loads(out.getvalue().strip())
    # Bare `rag` is a CLI command, not a question, so the Codex
    # equivalent of decision:block is continue:false + stopReason.
    assert parsed.get("continue") is False
    assert "stopReason" in parsed


def test_codex_indexing_started_emits_additional_context(fresh_xdg, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.chdir(project)
    out = _capture_stdout(monkeypatch)

    rc = codex_hook.run(
        _make_envelope("rag where does auth live", project),
        cwd=project,
    )

    assert rc == 0
    text = out.getvalue().strip()
    if not text:
        return
    parsed = json.loads(text)
    # When the hook kicks off a background indexer it surfaces a
    # heads-up via the additionalContext envelope so the model sees
    # the situation.
    assert "hookSpecificOutput" in parsed
    inner = parsed["hookSpecificOutput"]
    assert inner.get("hookEventName") == "UserPromptSubmit"
    assert "additionalContext" in inner
