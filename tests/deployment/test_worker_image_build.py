from __future__ import annotations

import re
from pathlib import Path


def test_sandbox_supervisor_cross_build_uses_native_go_toolchain() -> None:
    dockerfile = Path("docker/Dockerfile.worker").read_text(encoding="utf-8")
    stage = re.search(
        r"FROM --platform=\$BUILDPLATFORM \$\{GO_IMAGE\} AS sandbox-supervisor\n"
        r"(?P<body>.*?)(?=\nFROM )",
        dockerfile,
        flags=re.DOTALL,
    )

    assert stage is not None
    assert "ARG TARGETARCH" in stage["body"]
    assert "CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH}" in stage["body"]
