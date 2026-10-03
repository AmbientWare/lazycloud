"""What a container start imports before the first task."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

APP = """
import lazycloud

app = lazycloud.App("startup")


@app.function(image=lazycloud.Image(python_version="3.12"), cpu=0.25)
def hello(name: str = "world") -> str:
    return f"hello {name}"


@app.endpoint(route="/ping", methods=["POST"])
def ping(n: int = 0) -> int:
    return n
"""

# Loaded on the first call that needs them, never to declare an app.
CALL_ONLY = (
    "asyncio",
    "httpx",
    "lazycloud.clients.api",
    "lazycloud.contracts.api",
    "lazycloud.session.task",
    "lazycloud.terminal",
    "PIL",
    "rich",
    "urllib.request",
)


def test_runner_and_a_declared_app_load_no_client_terminal_or_api_models(tmp_path: Path) -> None:
    (tmp_path / "startup_app.py").write_text(APP)
    probe = "import sys, runner.protocol, startup_app; print(json.dumps(sorted(sys.modules)))"
    result = subprocess.run(
        [sys.executable, "-c", "import json; " + probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = set(json.loads(result.stdout))
    assert [name for name in CALL_ONLY if name in loaded] == []
