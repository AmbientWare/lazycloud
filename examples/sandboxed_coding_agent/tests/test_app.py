from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path

import pytest
from examples.sandboxed_coding_agent import app as agent_module
from examples.sandboxed_coding_agent.app import (
    MAX_PATCH_BYTES,
    PatchPlan,
    _bounded_output,
    _chat_completions_url,
    plan_patch,
    run_agent,
    validate_patch_path,
    validate_patch_plan,
)

from lazycloud import Sandbox, SandboxProcessResponse


@pytest.mark.parametrize(
    "value",
    ["", "/absolute.py", "../escape.py", "src/../escape.py", "a//b.py", "a\\b.py"],
)
def test_patch_paths_reject_absolute_traversal_and_ambiguous_values(value: str) -> None:
    with pytest.raises(ValueError):
        validate_patch_path(value)


def test_patch_plan_rejects_duplicates_unapproved_paths_and_binary_content() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        validate_patch_plan(
            {
                "files": [
                    {"path": "calculator.py", "content": "first"},
                    {"path": "calculator.py", "content": "second"},
                ]
            }
        )
    with pytest.raises(ValueError, match="not approved"):
        validate_patch_plan({"files": [{"path": "test_calculator.py", "content": "pass"}]})
    with pytest.raises(ValueError, match="UTF-8 text"):
        validate_patch_plan({"files": [{"path": "calculator.py", "content": "bad\x00data"}]})
    with pytest.raises(ValueError, match="UTF-8 text"):
        validate_patch_plan({"files": [{"path": "calculator.py", "content": "bad\x07data"}]})


def test_patch_plan_enforces_file_and_byte_limits() -> None:
    with pytest.raises(ValueError, match="1-3"):
        validate_patch_plan({"files": []})
    with pytest.raises(ValueError, match="bytes"):
        validate_patch_plan(
            {"files": [{"path": "calculator.py", "content": "x" * (MAX_PATCH_BYTES + 1)}]}
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://provider.example/v1",
        "https://user:password@provider.example/v1",
        "https://provider.example/v1?key=value",
        "https://provider.example/v1#fragment",
    ],
)
def test_provider_url_rejects_insecure_or_embedded_credentials(base_url: str) -> None:
    with pytest.raises(ValueError):
        _chat_completions_url(base_url)


class _ProviderResponse(io.BytesIO):
    def __enter__(self) -> _ProviderResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_planner_serializes_provider_request_and_validates_structured_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODING_AGENT_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("CODING_AGENT_MODEL", "coding-model")
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-api-key")
    requests: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, *, timeout: int) -> _ProviderResponse:
        requests.append(request)
        content = json.dumps(
            {
                "files": [
                    {
                        "path": "calculator.py",
                        "content": (
                            "def add(left: int, right: int) -> int:\n    return left + right\n"
                        ),
                    }
                ]
            }
        )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        return _ProviderResponse(body)

    monkeypatch.setattr(agent_module.urllib.request, "urlopen", urlopen)
    seed = {
        "calculator.py": "def add(left, right): return left - right\n",
        "test_calculator.py": "assert add(2, 3) == 5\n",
    }

    result = plan_patch.local("Fix addition", seed)

    assert result["files"][0]["path"] == "calculator.py"
    request = requests[0]
    assert request.full_url == "https://provider.example/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer test-api-key"
    request_data = request.data
    assert isinstance(request_data, bytes)
    payload = json.loads(request_data)
    assert payload["model"] == "coding-model"
    assert payload["response_format"] == {"type": "json_object"}


def test_planner_rejects_invalid_provider_json_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODING_AGENT_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("CODING_AGENT_MODEL", "coding-model")
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-api-key")

    def urlopen(request: urllib.request.Request, *, timeout: int) -> _ProviderResponse:
        del request
        return _ProviderResponse(b'{"provider":"untrusted-body"}')

    monkeypatch.setattr(agent_module.urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError) as error:
        plan_patch.local(
            "Fix addition",
            {"calculator.py": "bad", "test_calculator.py": "test"},
        )

    assert "untrusted-body" not in str(error.value)


def test_planner_bounds_direct_seed_inputs_before_calling_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def provider_patch(prompt: str, seed_files: dict[str, str]) -> PatchPlan:
        raise AssertionError("oversized seed must not reach the provider")

    monkeypatch.setattr(agent_module, "_provider_patch", provider_patch)

    with pytest.raises(ValueError, match="24000"):
        plan_patch.local(
            "Fix addition",
            {"calculator.py": "x" * 24_001, "test_calculator.py": "test"},
        )


def test_test_output_is_bounded_and_marks_truncation() -> None:
    output = _bounded_output("😀" * 5_000, "discarded-tail")

    assert len(output.encode()) <= 12_000
    assert "truncated" in output
    assert "discarded-tail" not in output


class _FakeFileSystem:
    def __init__(self) -> None:
        self.uploads: dict[str, str] = {}

    def create_directory(self, sandbox_path: str) -> None:
        del sandbox_path

    def upload_file(self, local_path: str | Path, sandbox_path: str) -> None:
        self.uploads[sandbox_path] = Path(local_path).read_text(encoding="utf-8")


class _FakeSandboxInstance:
    def __init__(self, *, process_error: RuntimeError | None = None) -> None:
        self.fs = _FakeFileSystem()
        self.process_error = process_error
        self.terminated = False

    def sandbox_id(self) -> str:
        return "sandbox-example"

    def run(
        self,
        command: list[str],
        *,
        timeout_seconds: float,
        cwd: str,
    ) -> SandboxProcessResponse:
        if self.process_error is not None:
            raise self.process_error
        return SandboxProcessResponse(
            pid=10,
            exit_code=0,
            stdout="",
            stderr="test_adds_two_integers ... ok\n",
        )

    def terminate(self) -> bool:
        self.terminated = True
        return True


def _valid_plan() -> PatchPlan:
    return {
        "files": [
            {
                "path": "calculator.py",
                "content": (
                    '"""Patched calculator."""\n\n'
                    "def add(left: int, right: int) -> int:\n"
                    "    return left + right\n"
                ),
            }
        ]
    }


def test_agent_uploads_patch_reports_process_result_and_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = _FakeSandboxInstance()
    monkeypatch.setenv("CODING_AGENT_API_KEY", "provider-credential-sentinel")

    def invoke_planner(prompt: str, seed_files: dict[str, str]) -> tuple[str, PatchPlan]:
        return "task-planner", _valid_plan()

    def create(
        self: Sandbox,
        *,
        stub_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> _FakeSandboxInstance:
        del self, stub_id
        return instance

    monkeypatch.setattr(agent_module, "_invoke_planner", invoke_planner)
    monkeypatch.setattr(Sandbox, "create", create)

    result = run_agent()

    assert result["planner_task_id"] == "task-planner"
    assert result["tests_passed"] is True
    assert result["changed_files"] == ["calculator.py"]
    assert (
        instance.fs.uploads["/workspace/project/calculator.py"]
        == _valid_plan()["files"][0]["content"]
    )
    assert "provider-credential-sentinel" not in "".join(instance.fs.uploads.values())
    assert "provider-credential-sentinel" not in json.dumps(result)
    assert instance.terminated is True


def test_orchestrator_terminates_the_sandbox_when_process_control_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = _FakeSandboxInstance(process_error=RuntimeError("process failed"))

    def invoke_planner(prompt: str, seed_files: dict[str, str]) -> tuple[str, PatchPlan]:
        del prompt, seed_files
        return "task-planner", _valid_plan()

    def create(
        self: Sandbox,
        *,
        stub_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> _FakeSandboxInstance:
        del self, stub_id, timeout_seconds
        return instance

    monkeypatch.setattr(agent_module, "_invoke_planner", invoke_planner)
    monkeypatch.setattr(Sandbox, "create", create)

    with pytest.raises(RuntimeError, match="process failed"):
        run_agent()

    assert instance.terminated is True
