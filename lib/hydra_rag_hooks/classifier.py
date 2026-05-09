"""Classify a file as code, prose, or skip.

Cheap and explicit: extension first, then canonical basename, then
shebang sniff. Same shape as hydra-llm.
"""

from __future__ import annotations

from pathlib import Path

CODE_EXTS = {
    ".py", ".pyi",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".scala",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".hh",
    ".cs", ".swift", ".m", ".mm",
    ".rb", ".php", ".pl", ".lua", ".r",
    ".sh", ".bash", ".zsh", ".fish",
    ".sql", ".html", ".css", ".scss", ".sass", ".less",
    ".vue", ".svelte",
    ".yaml", ".yml", ".toml", ".json", ".jsonc", ".ini",
    ".dockerfile", ".tf", ".hcl",
    ".cmake", ".mk",
    ".vim", ".el", ".lisp", ".clj", ".cljs", ".ex", ".exs",
}

PROSE_EXTS = {
    ".md", ".markdown", ".rst", ".txt", ".adoc", ".asciidoc",
    ".org", ".tex",
}

CODE_BASENAMES = {
    "Makefile", "makefile", "GNUmakefile",
    "Dockerfile", "Containerfile",
    "Justfile", "justfile",
    "Rakefile", "Gemfile",
    "Pipfile", "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "tsconfig.json",
    "build.gradle", "settings.gradle", "pom.xml",
    "Cargo.toml",
    "go.mod", "go.sum",
    "CMakeLists.txt",
    ".gitignore", ".dockerignore", ".gitattributes",
    ".editorconfig",
}

PROSE_BASENAMES = {
    "README", "readme", "CHANGELOG", "changelog",
    "LICENSE", "license", "COPYING", "AUTHORS", "CONTRIBUTORS",
    "NOTICE", "TODO", "NEWS",
}

# Filenames that always get skipped regardless of extension.
SKIP_BASENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "uv.lock",
    "Cargo.lock", "composer.lock",
    "go.sum",
}

# Top-level directory names (or anywhere along the path) that we never
# walk into.
SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", "bower_components",
    ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox",
    "target", "build", "dist", "out", ".next", ".nuxt",
    ".gradle", ".idea", ".vscode",
    ".cache", ".turbo",
    ".claude-rag-index", ".hydra-index",
    # Claude Code per-project state (skills, commands, settings, agents,
    # session caches). Indexing this would dump skill markdown and
    # internal config into the LanceDB; never useful retrieval and adds
    # noise. The hook never reads these for retrieval anyway.
    ".claude", ".claude-dev",
}

BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".mp3", ".mp4", ".wav", ".flac", ".ogg", ".webm", ".mov", ".avi", ".mkv",
    ".so", ".dylib", ".dll", ".exe", ".o", ".a", ".class", ".jar",
    ".bin", ".dat", ".pyc", ".pyo",
    ".gguf", ".safetensors", ".onnx", ".ckpt", ".pt", ".pth", ".npy", ".npz",
    ".db", ".sqlite", ".sqlite3",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
}


def classify(path: Path) -> str | None:
    """Return 'code', 'prose', or None to skip the file."""
    name = path.name
    if name in SKIP_BASENAMES:
        return None
    suffix = path.suffix.lower()
    if suffix in BINARY_EXTS:
        return None
    if suffix in CODE_EXTS:
        return "code"
    if suffix in PROSE_EXTS:
        return "prose"
    if name in CODE_BASENAMES:
        return "code"
    if name in PROSE_BASENAMES:
        return "prose"
    # Files starting with the canonical prose names (e.g. README.md
    # already handled by suffix, but bare README is here).
    stem_lower = path.stem.lower()
    if stem_lower in {"readme", "changelog", "license", "copying", "authors", "contributors", "notice"}:
        return "prose"
    # Shebang sniff for extensionless executables.
    if not suffix and path.is_file():
        try:
            with path.open("rb") as f:
                head = f.read(80)
            if head.startswith(b"#!"):
                return "code"
        except OSError:
            return None
    return None
