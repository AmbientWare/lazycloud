"""Plan a bounded code patch remotely and test it in an isolated Sandbox."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import TypedDict
from urllib.parse import urlsplit

from lazycloud.json_contracts import JsonValue, parse_json_object, parse_json_value

from lazycloud import App, Client, Image

APP_NAME = "sandboxed_coding_agent"
PROVIDER_SECRET_NAMES = (
    "CODING_AGENT_BASE_URL",
    "CODING_AGENT_MODEL",
    "CODING_AGENT_API_KEY",
)
MAX_PATCH_FILES = 3
MAX_PATCH_BYTES = 12_000
MAX_PROVIDER_RESPONSE_BYTES = 64_000
MAX_TEST_OUTPUT_BYTES = 12_000
APPROVED_PATCH_PATHS = frozenset({"calculator.py"})
PROJECT_ROOT = Path(__file__).with_name("project")
SANDBOX_PROJECT_ROOT = PurePosixPath("/workspace/project")

app = App(APP_NAME)
planner_image = Image(python_version="3.12")
sandbox_image = Image(python_version="3.12")


class PatchFile(TypedDict):
    path: str
    content: str


class PatchPlan(TypedDict):
    files: list[PatchFile]


class AgentResult(TypedDict):
    planner_task_id: str
    sandbox_id: str
    tests_passed: bool
    test_exit_code: int
    test_output: str
    changed_files: list[str]


class SandboxInspection(TypedDict):
    id: str
    status: str
    created_at: str


def validate_patch_path(value: str) -> str:
    """Accept only explicitly editable, POSIX-relative project files."""
    if not value or len(value) > 160 or "\\" in value or "\x00" in value:
        raise ValueError("patch paths must be non-empty POSIX-relative paths")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("patch paths cannot contain empty, dot, or parent segments")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError("patch paths must be relative")
    normalized = path.as_posix()
    if normalized not in APPROVED_PATCH_PATHS:
        raise ValueError(f"patch path is not approved: {normalized}")
    return normalized


def validate_patch_plan(value: JsonValue) -> PatchPlan:
    """Validate the provider's complete structured response and byte budget."""
    if not isinstance(value, dict) or set(value) != {"files"}:
        raise ValueError("patch plan must contain only a files array")
    raw_files = value["files"]
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= MAX_PATCH_FILES:
        raise ValueError(f"patch plan must contain 1-{MAX_PATCH_FILES} files")

    files: list[PatchFile] = []
    seen: set[str] = set()
    total_bytes = 0
    for raw_file in raw_files:
        if not isinstance(raw_file, dict) or set(raw_file) != {"path", "content"}:
            raise ValueError("each patch file must contain only path and content")
        path_value = raw_file["path"]
        content_value = raw_file["content"]
        if not isinstance(path_value, str) or not isinstance(content_value, str):
            raise ValueError("patch paths and content must be strings")
        path = validate_patch_path(path_value)
        if path in seen:
            raise ValueError(f"duplicate patch path: {path}")
        seen.add(path)
        if "\x00" in content_value or any(
            ord(character) < 32 and character not in "\n\r\t" for character in content_value
        ):
            raise ValueError(f"patch content must be UTF-8 text: {path}")
        try:
            content_bytes = content_value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"patch content must be UTF-8 text: {path}") from exc
        total_bytes += len(content_bytes)
        if total_bytes > MAX_PATCH_BYTES:
            raise ValueError(f"patch content exceeds {MAX_PATCH_BYTES} bytes")
        files.append({"path": path, "content": content_value})
    return {"files": files}


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required planner configuration is missing: {name}")
    return value


def _chat_completions_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("CODING_AGENT_BASE_URL must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("CODING_AGENT_BASE_URL cannot contain credentials, query, or fragment")
    return f"{base_url.rstrip('/')}/chat/completions"


def _provider_patch(prompt: str, seed_files: dict[str, str]) -> PatchPlan:
    base_url = _required_environment("CODING_AGENT_BASE_URL")
    model = _required_environment("CODING_AGENT_MODEL")
    api_key = _required_environment("CODING_AGENT_API_KEY")
    if len(model) > 160 or any(ord(character) < 32 for character in model):
        raise ValueError("CODING_AGENT_MODEL is invalid")

    user_message = json.dumps(
        {
            "request": prompt,
            "editable_paths": sorted(APPROVED_PATCH_PATHS),
            "files": seed_files,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    payload = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only JSON with exactly this shape: "
                        '{"files":[{"path":"calculator.py","content":"complete file"}]}. '
                        "Return complete UTF-8 file contents, edit only an editable path, "
                        "and do not use Markdown fences."
                    ),
                },
                {"role": "user", "content": user_message},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        _chat_completions_url(base_url),
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw_response = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        raise RuntimeError("planner provider request failed") from None
    if len(raw_response) > MAX_PROVIDER_RESPONSE_BYTES:
        raise RuntimeError("planner provider response exceeded the size limit")

    try:
        response_value = parse_json_object(raw_response)
        choices = response_value.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError
        plan_value = parse_json_value(content)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise RuntimeError("planner provider returned an invalid structured response") from None
    return validate_patch_plan(plan_value)


@app.function(
    name="plan-patch",
    image=planner_image,
    cpu=0.5,
    memory="256Mi",
    timeout_seconds=120,
    retries=0,
    secrets=list(PROVIDER_SECRET_NAMES),
)
def plan_patch(prompt: str, seed_files: dict[str, str]) -> PatchPlan:
    """Ask the configured provider for a validated, bounded patch plan."""
    try:
        prompt_bytes = prompt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("prompt must be UTF-8 text") from exc
    if not prompt.strip() or len(prompt_bytes) > 4_000:
        raise ValueError("prompt must contain 1-4000 UTF-8 bytes")
    if set(seed_files) != {"calculator.py", "test_calculator.py"}:
        raise ValueError("seed project must contain the expected calculator files")
    seed_bytes = 0
    for path, content in seed_files.items():
        if "\x00" in content:
            raise ValueError(f"seed project must contain UTF-8 text: {path}")
        try:
            seed_bytes += len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError(f"seed project must contain UTF-8 text: {path}") from exc
    if seed_bytes > 24_000:
        raise ValueError("seed project exceeds 24000 UTF-8 bytes")
    return _provider_patch(prompt, seed_files)


sandbox = app.sandbox(
    name="test-patch",
    image=sandbox_image,
    cpu=1,
    memory="512Mi",
    keep_warm_seconds=300,
    authorized=True,
    block_network=True,
    secrets=[],
    env={},
)


def _seed_files() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(PROJECT_ROOT.iterdir())
        if path.is_file()
    }


def _invoke_planner(prompt: str, seed_files: dict[str, str]) -> tuple[str, PatchPlan]:
    client = Client()
    target = client.deployment.resolve_target(
        kind=plan_patch.spec().kind,
        name=plan_patch.resource_name,
        app=APP_NAME,
    )
    submission = client.submit_deployment(target.deployment_id, prompt, seed_files)
    if submission.task is None:
        raise RuntimeError("planner deployment returned no Task")
    result = submission.task.wait(timeout_seconds=180, poll_interval_seconds=1)
    if not result.ok:
        raise RuntimeError(result.error or "planner Task failed")
    return submission.task_id, validate_patch_plan(result.task.result)


def _bounded_output(stdout: str, stderr: str) -> str:
    encoded = (stdout + stderr).encode("utf-8", errors="replace")
    if len(encoded) <= MAX_TEST_OUTPUT_BYTES:
        return encoded.decode("utf-8")
    suffix = b"\n... output truncated ...\n"
    available = MAX_TEST_OUTPUT_BYTES - len(suffix)
    return encoded[:available].decode("utf-8", errors="ignore") + suffix.decode("utf-8")


def inspect_sandboxes(limit: int = 20) -> list[SandboxInspection]:
    """List recent instances created from this example's Sandbox definition."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    return [
        {
            "id": row.id,
            "status": row.status.value,
            "created_at": row.created_at.isoformat(),
        }
        for row in sandbox.list(limit=limit)
        if row.name == sandbox.name
    ]


def run_agent(
    prompt: str = "Fix calculator.add so the test passes. Change only calculator.py.",
) -> AgentResult:
    """Invoke the trusted planner, test its patch, and always terminate the Sandbox."""
    seed_files = _seed_files()
    planner_task_id, plan = _invoke_planner(prompt, seed_files)
    instance = sandbox.create(timeout_seconds=180)
    try:
        instance.fs.create_directory(str(SANDBOX_PROJECT_ROOT))
        for path in sorted(PROJECT_ROOT.iterdir()):
            if path.is_file():
                instance.fs.upload_file(path, str(SANDBOX_PROJECT_ROOT / path.name))

        with tempfile.TemporaryDirectory(prefix="lazycloud-agent-patch-") as temp_dir:
            temp_root = Path(temp_dir)
            for patch_file in plan["files"]:
                local_path = temp_root / patch_file["path"]
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_text(patch_file["content"], encoding="utf-8")
                instance.fs.upload_file(
                    local_path,
                    str(SANDBOX_PROJECT_ROOT / patch_file["path"]),
                )

        test_result = instance.run(
            ["python", "-m", "unittest", "-v"],
            timeout_seconds=60,
            cwd=str(SANDBOX_PROJECT_ROOT),
        )
        return {
            "planner_task_id": planner_task_id,
            "sandbox_id": instance.sandbox_id(),
            "tests_passed": test_result.exit_code == 0,
            "test_exit_code": test_result.exit_code,
            "test_output": _bounded_output(test_result.stdout, test_result.stderr),
            "changed_files": [patch_file["path"] for patch_file in plan["files"]],
        }
    finally:
        instance.terminate()


__all__ = [
    "APPROVED_PATCH_PATHS",
    "APP_NAME",
    "MAX_PATCH_BYTES",
    "MAX_PATCH_FILES",
    "PROVIDER_SECRET_NAMES",
    "app",
    "inspect_sandboxes",
    "plan_patch",
    "run_agent",
    "sandbox",
    "validate_patch_path",
    "validate_patch_plan",
]
