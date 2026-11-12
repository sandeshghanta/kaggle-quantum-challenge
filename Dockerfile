# One image for every service and for the disposable lab-assistant job containers.
# Tool versions are pinned here; bump them deliberately.
FROM docker:29.1.3-cli AS dockercli
FROM ghcr.io/astral-sh/uv:0.11.10 AS uv

# Python environment, built where a compiler is available (kickmix compiles from source on some platforms).
FROM node:22-bookworm-slim AS pyenv
RUN apt-get update && apt-get install -y --no-install-recommends g++ && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
# one compile job at a time: kickmix's C++ needs a lot of memory per job, and small builders run out
ENV NPY_NUM_BUILD_JOBS=1 MAX_JOBS=1 CMAKE_BUILD_PARALLEL_LEVEL=1 UV_CONCURRENT_BUILDS=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM node:22-bookworm-slim

ARG GIT_SHA=nogit
ENV LOOP_GIT_SHA=${GIT_SHA}
ARG PI_VERSION=1.5.2
ARG CLAUDE_CODE_VERSION=2.1.322
ENV PI_CLAUDE_BRIDGE_VERSION=0.12.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git socat \
 && rm -rf /var/lib/apt/lists/*

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
RUN npm install -g --no-fund --no-audit \
      @earendil-works/pi-coding-agent@${PI_VERSION} @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
 && npm cache clean --force

COPY --from=pyenv /opt/python /opt/python
COPY --from=pyenv /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY . .

# Every process runs as an unprivileged user; job containers additionally get a read-only root.
# Mount points exist here so new named volumes start out owned by that user.
RUN useradd --uid 10001 --create-home --home-dir /home/loop loop \
 && mkdir -p /data /state /status \
 && chown 10001:10001 /data /state /status
USER 10001
ENV HOME=/home/loop
