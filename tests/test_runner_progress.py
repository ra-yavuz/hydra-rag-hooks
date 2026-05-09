"""Test the runner's _on_progress message parser.

The indexer emits throttled per-file "progress: i/N files" lines
during the embed loop. The runner must parse those into the live
files_done/files_total counters in the .progress file. Without this,
bare `rag` reports "0/N files" forever on big repos and looks wedged.
"""

from __future__ import annotations

from hydra_rag_hooks import progress as progress_mod


def _make_callback(index_dir):
    """Reproduces the runner's _on_progress closure as a standalone
    function so we can test the parsing without spawning a real
    indexer. Keep this in sync with runner._run_inline."""
    def _on_progress(msg: str) -> None:
        cur = progress_mod.read(index_dir)
        cur.message = msg
        if msg.startswith("walk:") or msg.startswith("embed:"):
            parts = msg.split()
            try:
                cur.files_total = int(parts[1])
            except (IndexError, ValueError):
                pass
        elif msg.startswith("progress:"):
            parts = msg.split()
            if len(parts) >= 2 and "/" in parts[1]:
                a, _, b = parts[1].partition("/")
                try:
                    cur.files_done = int(a)
                    cur.files_total = int(b)
                except ValueError:
                    pass
        progress_mod.write(index_dir, cur)
    return _on_progress


def test_walk_message_sets_files_total(tmp_path):
    progress_mod.write(tmp_path, progress_mod.Progress(state="indexing"))
    cb = _make_callback(tmp_path)
    cb("walk: 3815 candidate files")
    p = progress_mod.read(tmp_path)
    assert p.files_total == 3815
    assert p.files_done == 0


def test_embed_message_sets_files_total(tmp_path):
    progress_mod.write(tmp_path, progress_mod.Progress(state="indexing"))
    cb = _make_callback(tmp_path)
    cb("embed: 3815 files to (re)index")
    p = progress_mod.read(tmp_path)
    assert p.files_total == 3815


def test_progress_message_increments_files_done(tmp_path):
    progress_mod.write(tmp_path, progress_mod.Progress(state="indexing", files_total=3815))
    cb = _make_callback(tmp_path)
    cb("progress: 1240/3815 files")
    p = progress_mod.read(tmp_path)
    assert p.files_done == 1240
    assert p.files_total == 3815


def test_progress_message_can_arrive_before_walk(tmp_path):
    # If the indexer emits a progress line before any walk/embed line
    # (eg. resumed run), the parser should still pick up both numbers
    # from the slash-separated form.
    progress_mod.write(tmp_path, progress_mod.Progress(state="indexing"))
    cb = _make_callback(tmp_path)
    cb("progress: 16/100 files")
    p = progress_mod.read(tmp_path)
    assert p.files_done == 16
    assert p.files_total == 100


def test_malformed_progress_message_is_ignored(tmp_path):
    progress_mod.write(tmp_path, progress_mod.Progress(state="indexing", files_done=42, files_total=99))
    cb = _make_callback(tmp_path)
    cb("progress: garbage")
    p = progress_mod.read(tmp_path)
    # Untouched: garbage in the message field must not corrupt counters.
    assert p.files_done == 42
    assert p.files_total == 99
    assert p.message == "progress: garbage"
