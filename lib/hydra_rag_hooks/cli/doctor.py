"""crh doctor - diagnose the install.

Walks a checklist and prints pass/warn/fail for each item. Exit 0 if
everything passes (only "ok" or "info" items), 1 if there are warnings,
2 if there are failures.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .. import paths, progress as progress_mod, registry
from . import _common


_OK = "ok"
_WARN = "warn"
_FAIL = "fail"
_INFO = "info"


def _check(label: str, severity: str, detail: str = "") -> dict:
    return {"check": label, "severity": severity, "detail": detail}


def _check_managed_settings_wired() -> dict:
    p = Path("/etc/claude-code/managed-settings.json")
    if not p.exists():
        return _check(
            "hook wired into managed-settings.json",
            _WARN,
            f"{p} does not exist; hook is not wired. apt install would create it. "
            f"Use `hydra-rag-hooks-admin install` if you installed by hand.",
        )
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return _check("hook wired into managed-settings.json", _FAIL, f"could not read {p}: {e}")
    matchers = (data.get("hooks") or {}).get("UserPromptSubmit") or []
    cmd_substr = "hydra-rag-hooks"
    if any(
        any(cmd_substr in (h.get("command") or "") for h in (m.get("hooks") or []) if isinstance(h, dict))
        for m in matchers if isinstance(m, dict)
    ):
        return _check("hook wired into managed-settings.json", _OK, str(p))
    return _check(
        "hook wired into managed-settings.json",
        _FAIL,
        f"no UserPromptSubmit entry referencing hydra-rag-hooks in {p}",
    )


def _check_hook_binary() -> dict:
    p = Path("/usr/lib/hydra-rag-hooks/hydra-rag-hooks-claude-hook")
    if not p.is_file():
        return _check("hook binary present", _FAIL, f"{p} missing")
    if not os.access(p, os.X_OK):
        return _check("hook binary present", _FAIL, f"{p} not executable")
    return _check("hook binary present", _OK, str(p))


def _check_fastembed_importable() -> dict:
    try:
        import fastembed  # noqa: F401
        return _check("fastembed importable", _OK, f"version {fastembed.__version__}")
    except ImportError as e:
        return _check(
            "fastembed importable",
            _WARN,
            f"{e}. Install with `pip install --user fastembed lancedb pyarrow` "
            f"or pick another embedder backend in config.",
        )


def _check_lancedb_importable() -> dict:
    try:
        import lancedb  # noqa: F401
        return _check("lancedb importable", _OK)
    except ImportError as e:
        return _check(
            "lancedb importable",
            _WARN,
            f"{e}. Install with `pip install --user lancedb pyarrow`.",
        )


def _check_model_cache() -> dict:
    cache = paths.models_cache_dir()
    if not cache.is_dir():
        return _check("model cache present", _WARN, f"{cache} missing")
    onnx = list(cache.rglob("*.onnx"))
    if not onnx:
        return _check(
            "model cache present",
            _WARN,
            f"{cache} exists but contains no .onnx file. "
            f"First `rag <q>` or `crh index` will fetch ~80MB.",
        )
    total = sum(p.stat().st_size for p in onnx)
    return _check(
        "model cache present",
        _OK,
        f"{cache} ({_common.human_bytes(total)} of .onnx)",
    )


def _check_embedder_socket() -> dict:
    sock = paths.daemon_socket()
    if sock.exists():
        return _check("embedder daemon socket", _INFO, f"{sock} present (warm daemon may be running)")
    return _check("embedder daemon socket", _INFO, f"{sock} absent (daemon will start on first retrieval)")


def _check_orphan_indexers() -> dict:
    """Look for indexer processes whose .progress file claims indexing
    but whose pid is dead, or .progress files older than ~24h."""
    issues = []
    for entry in registry.load():
        index_dir = Path(entry.path) / paths.INDEX_DIR_NAME
        if not (index_dir / progress_mod.PROGRESS_FILE).exists():
            continue
        prog = progress_mod.read(index_dir)
        if prog.state in ("indexing", "refreshing"):
            try:
                os.kill(prog.pid, 0)
            except OSError:
                issues.append(
                    f"  - {index_dir}: stale .progress (pid {prog.pid} dead)"
                )
    if issues:
        return _check(
            "no orphan indexers",
            _WARN,
            "stale .progress files found:\n" + "\n".join(issues) +
            "\n  Recover: rm those .progress files, then `crh refresh`.",
        )
    return _check("no orphan indexers", _OK)


def _check_disk_usage() -> dict:
    total = 0
    n = 0
    for entry in registry.load():
        index_dir = Path(entry.path) / paths.INDEX_DIR_NAME
        if not index_dir.is_dir():
            continue
        for f in index_dir.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        n += 1
    if n == 0:
        return _check("index disk usage", _INFO, "no registered stores")
    return _check(
        "index disk usage",
        _OK if total < 5 * 1024 * 1024 * 1024 else _WARN,
        f"{n} stores, {_common.human_bytes(total)} on disk",
    )


def _check_systemctl_user() -> dict:
    if not shutil.which("systemctl"):
        return _check("systemctl available", _INFO, "not on $PATH; refresher daemon controls won't work")
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", "hydra-rag-hooks-refresher.service"],
            capture_output=True, text=True, timeout=2,
        )
        state = out.stdout.strip() or "(no output)"
    except (subprocess.SubprocessError, OSError) as e:
        return _check("refresher daemon", _INFO, f"could not query systemd: {e}")
    return _check("refresher daemon", _INFO, f"systemd state: {state}")


_CHECKS = (
    _check_hook_binary,
    _check_managed_settings_wired,
    _check_fastembed_importable,
    _check_lancedb_importable,
    _check_model_cache,
    _check_embedder_socket,
    _check_orphan_indexers,
    _check_disk_usage,
    _check_systemctl_user,
)


def run(args) -> int:
    results = [chk() for chk in _CHECKS]
    if args.json:
        _common.emit_json(results)
    else:
        sym = {_OK: "ok  ", _WARN: "warn", _FAIL: "FAIL", _INFO: "info"}
        for r in results:
            tag = sym.get(r["severity"], r["severity"])
            line = f"  [{tag}] {r['check']}"
            if r["detail"]:
                line += f"  {r['detail']}"
            print(line)

    if any(r["severity"] == _FAIL for r in results):
        return 2
    if any(r["severity"] == _WARN for r in results):
        return 1
    return 0
