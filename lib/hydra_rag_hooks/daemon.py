"""Warm embedder daemon.

A small persistent process that keeps the embedder loaded and answers
embed requests over a Unix domain socket. The hook spawns it on first
use; it idles out after `daemon.idle_ttl_seconds` and respawns on the
next call.

Wire protocol: newline-delimited JSON, one request per line.

    request:  {"op": "embed_query" | "embed_documents", "texts": [...]}
              {"op": "ping"}
              {"op": "shutdown"}
    response: {"ok": true, "vectors": [...], "dim": <int>}
              {"ok": false, "error": "..."}
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import paths
from .embedder import resolve as resolve_embedder, Embedder


def _is_running(pidfile: Path) -> bool:
    if not pidfile.exists():
        return False
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _write_pidfile(pidfile: Path) -> None:
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(f"{os.getpid()}\n")


def _socket_path() -> Path:
    return paths.daemon_socket()


def is_alive(timeout: float = 0.2) -> bool:
    sock_path = _socket_path()
    if not sock_path.exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
        s.sendall(b'{"op":"ping"}\n')
        data = s.recv(1024)
        return b'"ok": true' in data or b'"ok":true' in data
    except (OSError, socket.timeout):
        return False
    finally:
        s.close()


def call(op: str, payload: dict[str, Any] | None = None, timeout: float = 60.0) -> dict[str, Any]:
    sock_path = _socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
        msg = {"op": op}
        if payload:
            msg.update(payload)
        s.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        line = buf.split(b"\n", 1)[0]
        return json.loads(line.decode("utf-8"))
    finally:
        s.close()


def spawn(detach: bool = True) -> None:
    """Fork off a daemon process.

    Returns once the child has bound the socket (or raises after a timeout).
    """
    paths.ensure_dirs()
    sock_path = _socket_path()
    pidfile = paths.daemon_pidfile()
    if is_alive():
        return
    # Stale socket from a previous run.
    if sock_path.exists():
        try:
            sock_path.unlink()
        except OSError:
            pass

    if detach:
        pid = os.fork()
        if pid != 0:
            # Parent: wait for the child to come up.
            for _ in range(50):
                if is_alive():
                    return
                time.sleep(0.1)
            raise RuntimeError("embedder daemon failed to start within 5s")
        # Child: fully detach.
        os.setsid()
        # Close stdio so the parent shell does not hang on it.
        sys.stdin.close()
        log = paths.daemon_logfile()
        log.parent.mkdir(parents=True, exist_ok=True)
        f = open(log, "ab", buffering=0)
        os.dup2(f.fileno(), 1)
        os.dup2(f.fileno(), 2)

    _write_pidfile(pidfile)
    cfg = config_mod.load()
    try:
        emb = resolve_embedder(cfg.get("embedder", default={}) or {})
        # Eagerly load the model so the first request is fast.
        _ = emb.dim
    except Exception as e:
        print(f"daemon: failed to load embedder: {e}", file=sys.stderr, flush=True)
        os._exit(2)
    idle_ttl = float(cfg.get("daemon", "idle_ttl_seconds", default=1800) or 1800)
    serve(emb, idle_ttl)


def serve(embedder: Embedder, idle_ttl_seconds: float) -> None:
    sock_path = _socket_path()
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        try:
            sock_path.unlink()
        except OSError:
            pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    os.chmod(sock_path, 0o600)
    server.listen(8)
    server.settimeout(1.0)

    last_activity = time.monotonic()
    stop = threading.Event()

    def _shutdown(_signum=None, _frame=None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"daemon: serving on {sock_path}, embedder={embedder.kind}:{embedder.model} dim={embedder.dim}",
          file=sys.stderr, flush=True)

    try:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                if time.monotonic() - last_activity > idle_ttl_seconds:
                    print("daemon: idle timeout, shutting down", file=sys.stderr, flush=True)
                    break
                continue
            with conn:
                conn.settimeout(60.0)
                try:
                    buf = b""
                    while b"\n" not in buf:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                    if not buf:
                        continue
                    line = buf.split(b"\n", 1)[0]
                    req = json.loads(line.decode("utf-8"))
                    last_activity = time.monotonic()
                    resp = handle(embedder, req)
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                    if req.get("op") == "shutdown":
                        stop.set()
                        break
                except Exception as e:  # pragma: no cover - defensive
                    err = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    try:
                        conn.sendall((json.dumps(err) + "\n").encode("utf-8"))
                    except OSError:
                        pass
    finally:
        try:
            server.close()
        finally:
            try:
                if sock_path.exists():
                    sock_path.unlink()
            except OSError:
                pass
            try:
                pidfile = paths.daemon_pidfile()
                if pidfile.exists():
                    pidfile.unlink()
            except OSError:
                pass


def handle(embedder: Embedder, req: dict[str, Any]) -> dict[str, Any]:
    op = req.get("op")
    if op == "ping":
        return {"ok": True, "kind": embedder.kind, "model": embedder.model, "dim": embedder.dim}
    if op == "shutdown":
        return {"ok": True}
    if op == "embed_query":
        text = req.get("text")
        if not isinstance(text, str):
            return {"ok": False, "error": "text must be a string"}
        v = embedder.embed_query(text)
        return {"ok": True, "vector": v, "dim": embedder.dim}
    if op == "embed_documents":
        texts = req.get("texts") or []
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            return {"ok": False, "error": "texts must be a list of strings"}
        vs = embedder.embed_documents(texts)
        return {"ok": True, "vectors": vs, "dim": embedder.dim}
    return {"ok": False, "error": f"unknown op: {op!r}"}


def stop_daemon() -> bool:
    if not is_alive():
        return False
    try:
        call("shutdown", timeout=2.0)
    except OSError:
        pass
    # Best-effort wait.
    for _ in range(20):
        if not is_alive():
            return True
        time.sleep(0.1)
    return not is_alive()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hydra-rag-hooksd",
                                description="Warm embedder daemon for hydra-rag-hooks.")
    p.add_argument("--foreground", action="store_true",
                   help="Run in the foreground (do not fork).")
    args = p.parse_args(argv)
    spawn(detach=not args.foreground)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
