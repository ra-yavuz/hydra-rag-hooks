# claude-rag-hook dev sandbox.
#
# Toolchain lives in this image, never on the host. The repo is mounted
# at /opt/claude-rag-hook by docker-compose; pip install -e is done at
# container start so edits to lib/ on the host are live inside.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates jq ripgrep file less wget \
        build-essential dpkg-dev fakeroot lintian \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        pyyaml numpy pathspec lancedb pyarrow \
        fastembed httpx \
        ruff pytest

WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

CMD ["bash"]
