"""crh export / crh import: portable index bundles.

Use case: a colleague has indexed a large monorepo and wants to share
the index so other people on the team don't pay the indexing cost.
The index is just a directory of files (LanceDB table + meta.yaml +
files manifest), so a tarball is enough.

Bundle shape:

    <project>.<embedder-slug>.<schema>.crh.tar.zst
      claude-rag-index/
        chunks.lance/...
        files.json
        meta.yaml
      bundle.json                 # version, source-project name, created-at

Why .tar.zst (or .tar.gz fallback): LanceDB tables compress well, but
indexes for big repos can be hundreds of MB raw. zstd is widely
available on modern Debian (`zstd` package) and gives ~3x better
compression than gzip on these files. Falls back to .tar.gz if zstd
is missing.

Import refuses to overwrite a populated `.claude-rag-index/` without
`--force`. After unpacking, the store is registered in stores.json
so `crh ls` and tag-federated queries see it. Embedder compatibility
check: if the imported index's `meta.yaml` lists an embedder different
from the user's configured one, we warn but proceed; the retrieval
path picks the right embedder per-index from meta.yaml so mixed
indexes coexist.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import yaml

from .. import config as config_mod, paths, registry
from . import _common


BUNDLE_SUFFIX_ZST = ".crh.tar.zst"
BUNDLE_SUFFIX_GZ = ".crh.tar.gz"
BUNDLE_VERSION = 1


def _slugify(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text or "")
    s = s.strip("-._") or "index"
    return s[:64]


def _have_zstd() -> bool:
    return shutil.which("zstd") is not None


def _read_meta(index_dir: Path) -> dict:
    p = index_dir / "meta.yaml"
    if not p.is_file():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _embedder_slug(meta: dict) -> str:
    emb = meta.get("embedder") or {}
    if not isinstance(emb, dict):
        return "unknown"
    model = emb.get("model") or emb.get("kind") or "unknown"
    return _slugify(str(model).replace("/", "-"))


def _bundle_filename(project: Path, meta: dict) -> str:
    project_slug = _slugify(project.name) or "index"
    embedder = _embedder_slug(meta)
    schema = str(meta.get("schema_version") or 1)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = BUNDLE_SUFFIX_ZST if _have_zstd() else BUNDLE_SUFFIX_GZ
    return f"{project_slug}.{embedder}.v{schema}.{ts}{suffix}"


def _write_bundle_metadata(staging: Path, project: Path, meta: dict) -> None:
    payload = {
        "bundle_version": BUNDLE_VERSION,
        "source_project_name": project.name,
        "source_project_path": str(project.resolve()),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "index_meta": meta,
    }
    (staging / "bundle.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8",
    )


def _create_tar_gz(out: Path, root: Path) -> None:
    with tarfile.open(out, "w:gz") as tar:
        tar.add(root, arcname=root.name)


def _create_tar_zst(out: Path, root: Path) -> None:
    # Pipe a streaming tar through `zstd -T0 -c` so we don't need an
    # extra temp file. zstd default level (3) is plenty fast on this
    # kind of data and meaningfully smaller than gzip.
    with out.open("wb") as f_out:
        proc = subprocess.Popen(
            ["zstd", "-T0", "-q", "-c"],
            stdin=subprocess.PIPE, stdout=f_out,
        )
        assert proc.stdin is not None
        try:
            with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
                tar.add(root, arcname=root.name)
        finally:
            proc.stdin.close()
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"zstd exited {rc}")


def _extract_archive(src: Path, dest: Path) -> None:
    suffix_zst = src.name.endswith(BUNDLE_SUFFIX_ZST) or src.name.endswith(".tar.zst")
    if suffix_zst:
        if not _have_zstd():
            raise RuntimeError(
                "this bundle is .tar.zst but the zstd binary is not on PATH. "
                "Install zstd and retry, or ask the sender to re-export with "
                "no zstd available so they fall back to .tar.gz."
            )
        # Decompress into a temp .tar so tarfile can seek normally; the
        # streaming `r|` mode rejects path-traversal-prevention seeks.
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            with tmp_path.open("wb") as f_out:
                rc = subprocess.run(
                    ["zstd", "-dc", str(src)],
                    stdout=f_out, check=False,
                ).returncode
            if rc != 0:
                raise RuntimeError(f"zstd -dc exited {rc}")
            with tarfile.open(tmp_path, "r:") as tar:
                _safe_extract(tar, dest)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return
    with tarfile.open(src, "r:*") as tar:
        _safe_extract(tar, dest)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Reject path-traversal entries before extracting.

    Use is_relative_to (Python 3.9+) instead of a string startswith
    check; startswith treats `/tmp/dest-abcd2/...` as a child of
    `/tmp/dest-abcd`, which would be a real escape. is_relative_to
    is path-component aware.
    """
    dest_resolved = dest.resolve()
    for member in tar:
        member_path = (dest / member.name).resolve()
        try:
            is_inside = member_path.is_relative_to(dest_resolved)
        except AttributeError:
            # Python <3.9 fallback (we declare 3.10 minimum, but be defensive).
            try:
                member_path.relative_to(dest_resolved)
                is_inside = True
            except ValueError:
                is_inside = False
        if not is_inside:
            raise RuntimeError(f"refusing to extract path traversal entry: {member.name}")
    tar.extractall(dest, filter="data")


# ---------------------------------------------------------------------------
# crh export
# ---------------------------------------------------------------------------


def run_export(args: argparse.Namespace) -> int:
    project = _common.resolve_path(args.path).resolve()
    index_dir = project / paths.INDEX_DIR_NAME
    if not index_dir.is_dir():
        print(
            f"crh export: no index at {index_dir}. Type `rag <q>` in the "
            f"project folder to build one, or pass a different path.",
            file=sys.stderr,
        )
        return 1

    meta = _read_meta(index_dir)

    if args.output:
        output = Path(args.output).expanduser().resolve()
        if output.is_dir():
            output = output / _bundle_filename(project, meta)
    else:
        output = Path.cwd() / _bundle_filename(project, meta)

    if output.exists() and not args.force:
        print(
            f"crh export: refusing to overwrite {output}. Pass --force to allow.",
            file=sys.stderr,
        )
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)

    # Stage the index into a temp dir so we can drop bundle.json next to it.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="crh-export-") as td:
        staging = Path(td) / "claude-rag-index-bundle"
        staging.mkdir()
        # Copy the index folder (preserve its on-disk shape under the
        # expected name `claude-rag-index/`).
        shutil.copytree(index_dir, staging / "claude-rag-index")
        _write_bundle_metadata(staging, project, meta)

        try:
            if _have_zstd():
                _create_tar_zst(output, staging)
            else:
                _create_tar_gz(output, staging)
        except Exception as e:  # noqa: BLE001
            print(f"crh export: failed: {type(e).__name__}: {e}", file=sys.stderr)
            try:
                output.unlink()
            except OSError:
                pass
            return 1

    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"exported {project} to {output} ({size_mb:.1f} MB)")
    print(
        "Send this file to a colleague; they can install it with "
        "`crh import <file>` from the matching project folder."
    )
    return 0


# ---------------------------------------------------------------------------
# crh import
# ---------------------------------------------------------------------------


def run_import(args: argparse.Namespace) -> int:
    src = Path(args.bundle).expanduser().resolve()
    if not src.is_file():
        print(f"crh import: not a file: {src}", file=sys.stderr)
        return 1

    project = _common.resolve_path(args.path).resolve()
    if not project.is_dir():
        print(f"crh import: not a directory: {project}", file=sys.stderr)
        return 1

    target_index = project / paths.INDEX_DIR_NAME
    if target_index.exists() and not args.force:
        print(
            f"crh import: {target_index} already exists. Pass --force to "
            f"overwrite (drops the existing index), or run `crh forget {project}` "
            f"first.",
            file=sys.stderr,
        )
        return 1

    import tempfile
    with tempfile.TemporaryDirectory(prefix="crh-import-") as td:
        staging = Path(td)
        try:
            _extract_archive(src, staging)
        except Exception as e:  # noqa: BLE001
            print(f"crh import: extract failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1

        # Bundle layout: claude-rag-index-bundle/{claude-rag-index/, bundle.json}
        roots = [p for p in staging.iterdir() if p.is_dir()]
        if not roots:
            print("crh import: bundle is empty.", file=sys.stderr)
            return 1
        bundle_root = roots[0]
        unpacked_index = bundle_root / "claude-rag-index"
        if not unpacked_index.is_dir():
            print(
                f"crh import: bundle does not contain claude-rag-index/ "
                f"(found {bundle_root.name}/{[p.name for p in bundle_root.iterdir()]}).",
                file=sys.stderr,
            )
            return 1

        bundle_meta_path = bundle_root / "bundle.json"
        if bundle_meta_path.is_file():
            try:
                bundle_meta = json.loads(bundle_meta_path.read_text())
            except json.JSONDecodeError:
                bundle_meta = {}
        else:
            bundle_meta = {}

        # Embedder-compat heads-up.
        idx_meta = _read_meta(unpacked_index)
        cfg = config_mod.load()
        local_emb = (cfg.get("embedder") or {})
        bundle_emb = idx_meta.get("embedder") or {}
        if isinstance(bundle_emb, dict) and bundle_emb.get("model") and \
                bundle_emb.get("model") != local_emb.get("model"):
            print(
                f"crh import: heads up: bundle was built with embedder "
                f"'{bundle_emb.get('model')}' (dim {bundle_emb.get('dim')}); "
                f"your config defaults to '{local_emb.get('model')}'. The "
                f"retrieval path reads the embedder from the index's "
                f"meta.yaml, so mixed indexes coexist; this is just a heads-up."
            )

        if target_index.exists():
            shutil.rmtree(target_index)
        shutil.move(str(unpacked_index), str(target_index))

    # Register in stores.json so `crh ls` and tag federation see the import.
    emb = idx_meta.get("embedder") or {}
    entry = registry.StoreEntry(
        path=str(project),
        embedder=str((emb.get("model") if isinstance(emb, dict) else None) or ""),
        dim=int((emb.get("dim") if isinstance(emb, dict) else 0) or 0),
    )
    registry.upsert(entry)

    src_label = bundle_meta.get("source_project_name") or "unknown"
    print(f"imported into {target_index}")
    print(f"  source project: {src_label}")
    print(f"  registered in stores.json; type `rag <question>` in {project} to use it.")
    return 0
