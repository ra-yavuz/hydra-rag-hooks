from pathlib import Path

from hydra_rag_hooks.classifier import classify


def test_python_is_code(tmp_path: Path):
    p = tmp_path / "foo.py"
    p.write_text("print('hi')\n")
    assert classify(p) == "code"


def test_markdown_is_prose(tmp_path: Path):
    p = tmp_path / "README.md"
    p.write_text("# hello\n")
    assert classify(p) == "prose"


def test_lockfile_skipped(tmp_path: Path):
    p = tmp_path / "package-lock.json"
    p.write_text("{}\n")
    assert classify(p) is None


def test_binary_skipped(tmp_path: Path):
    p = tmp_path / "image.png"
    p.write_bytes(b"\x89PNG")
    assert classify(p) is None


def test_makefile_basename(tmp_path: Path):
    p = tmp_path / "Makefile"
    p.write_text("all:\n\techo hi\n")
    assert classify(p) == "code"


def test_extensionless_shebang(tmp_path: Path):
    p = tmp_path / "myscript"
    p.write_text("#!/usr/bin/env bash\necho hi\n")
    assert classify(p) == "code"
