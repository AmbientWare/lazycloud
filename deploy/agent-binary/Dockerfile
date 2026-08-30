# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

# Debian 11 uses glibc 2.31, so the resulting binary runs on Amazon Linux 2023
# (glibc 2.34) without requiring Python on the customer machine.
ARG PYTHON_IMAGE=python:3.12.11-slim-bullseye@sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db
ARG AMAZON_LINUX_IMAGE=amazonlinux:2023@sha256:f590f33787c76bee9c42cc6826ee887bc1f3b60ba5ed137af450a6c0a79cdb57
ARG AGENT_RUNTIME_IMAGE=debian:bookworm-slim
ARG DOCKER_CLI_IMAGE=docker:27.5.1-cli
ARG NEWT_VERSION=1.16.0
ARG NEWT_AMD64_SHA256=edb2e3d68549e009efdd461939db80c8ae43915fae89de8a332f8a29e00c92a8
ARG NEWT_ARM64_SHA256=2be3a8122478330471d531fcc5668519d20654550a9e6a9fa2f76b89e15de965
FROM ${PYTHON_IMAGE} AS agent-artifact-build

ARG TARGETARCH
ARG PYINSTALLER_VERSION=6.16.0

ENV PATH="/workspace/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    SOURCE_DATE_EPOCH=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_HTTP_TIMEOUT=120 \
    UV_LINK_MODE=copy

WORKDIR /workspace

COPY --from=ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY apps ./apps

RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,id=agent-artifact-uv-${TARGETARCH},target=/root/.cache/uv \
    uv sync --frozen --no-dev --package agent-app --no-editable \
    && uv pip install --python /workspace/.venv/bin/python \
        "altgraph==0.17.5" \
        "packaging==26.2" \
        "pyinstaller==${PYINSTALLER_VERSION}" \
        "pyinstaller-hooks-contrib==2026.6" \
        "setuptools==83.0.0"

RUN case "${TARGETARCH}" in amd64|arm64) ;; \
      *) echo "unsupported agent artifact architecture: ${TARGETARCH}" >&2; exit 2 ;; \
    esac \
    && python -m PyInstaller \
        --clean \
        --noconfirm \
        --onefile \
        --strip \
        --name "lazycloud-agent-linux-${TARGETARCH}" \
        --distpath /out \
        --workpath /tmp/pyinstaller-build \
        --specpath /tmp/pyinstaller-spec \
        apps/agent/src/agent_app/main.py \
    && "/out/lazycloud-agent-linux-${TARGETARCH}" --help >/dev/null

FROM ${AMAZON_LINUX_IMAGE} AS agent-artifact-amazon-linux-test
ARG TARGETARCH
COPY --from=agent-artifact-build /out/ /out/
RUN "/out/lazycloud-agent-linux-${TARGETARCH}" --help >/dev/null

FROM scratch AS agent-artifact
COPY --from=agent-artifact-amazon-linux-test /out/ /

FROM ${DOCKER_CLI_IMAGE} AS docker-cli

FROM ${AGENT_RUNTIME_IMAGE} AS agent-runtime
ARG TARGETARCH
ARG NEWT_VERSION
ARG NEWT_AMD64_SHA256
ARG NEWT_ARM64_SHA256

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fuse3 \
        iproute2 \
        iptables \
    && case "${TARGETARCH}" in \
        amd64) NEWT_SHA256="${NEWT_AMD64_SHA256}" ;; \
        arm64) NEWT_SHA256="${NEWT_ARM64_SHA256}" ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 2 ;; \
    esac \
    && curl -fsSL \
        "https://github.com/fosrl/newt/releases/download/${NEWT_VERSION}/newt_linux_${TARGETARCH}" \
        -o /usr/local/bin/newt \
    && echo "${NEWT_SHA256}  /usr/local/bin/newt" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/newt \
    && rm -rf /var/lib/apt/lists/*

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=agent-artifact-amazon-linux-test \
    "/out/lazycloud-agent-linux-${TARGETARCH}" \
    /usr/local/bin/lazycloud-agent

RUN docker --version \
    && newt --version \
    && lazycloud-agent --help >/dev/null

ENTRYPOINT ["lazycloud-agent"]
