# The platform's Python base, one image per PYTHON_VERSION: uv's managed
# build of that exact release (python-build-standalone) on Debian. Images
# without a base of their own build on it and containers without an image
# run it, so the server converts it at start (LAZYCLOUD_IMAGE_TEMPLATE).
# Build it with `docker buildx bake -f deploy/images/docker-bake.hcl python`.
# Logs are removed and timestamps clamped, so equal inputs build the same
# digest from one release to the next.

FROM docker.io/library/debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/* /var/log/*
ARG PYTHON_VERSION
ENV UV_PYTHON_INSTALL_DIR=/opt/runtime-python
# uv marks its Pythons externally managed; this one is the image's own, so
# pip installs into it as into a python image's. The standard library's
# bytecode uses unchecked hashes: imports never stat the sources, and
# clamped file times leave it valid.
RUN --mount=from=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c,source=/uv,target=/usr/local/bin/uv \
    set -eu; \
    export UV_NO_CACHE=1; \
    uv python install "$PYTHON_VERSION" --managed-python --no-bin; \
    python=$(uv python find "$PYTHON_VERSION" --managed-python --no-project); \
    "$python" -c "import sys; raise SystemExit(sys.version.split()[0] != '$PYTHON_VERSION')"; \
    "$python" -m pip --version >/dev/null 2>&1 || "$python" -m ensurepip; \
    rm -f "$("$python" -c "import sysconfig; print(sysconfig.get_path('stdlib'))")/EXTERNALLY-MANAGED"; \
    minor=${PYTHON_VERSION%.*}; \
    for link in python python3 "python$minor"; do ln -s "$python" "/usr/local/bin/$link"; done; \
    ln -s "$(dirname "$python")/pip" /usr/local/bin/pip; \
    "$python" -c "import compileall, py_compile, sysconfig; raise SystemExit(not compileall.compile_dir(sysconfig.get_path('stdlib'), force=True, quiet=1, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH))"
CMD ["python3"]
