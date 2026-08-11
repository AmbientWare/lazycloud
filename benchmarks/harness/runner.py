from __future__ import annotations

import shlex
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import ExitStack
from pathlib import Path

from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from foundation.process import run_process
from identity.users import UserService
from pydantic import JsonValue
from storage.service import CacheStorage, ObjectStorage
from storage_client.s3 import S3ObjectInfo

from benchmarks.harness.models import (
    BenchmarkKind,
    BenchmarkReport,
    BenchmarkResult,
    BenchmarkStatus,
    SuiteSpec,
)
from benchmarks.harness.sandbox_parallel import (
    SandboxParallelConfig,
    build_sandbox_parallel_plan,
)
from benchmarks.harness.suites import DEFAULT_CASES, SuiteLoader, case_for
from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


def _services(root: Path) -> ApiServices:
    redis_client = RedisClient.from_settings()
    binary_redis_client = RedisClient.from_settings(decode_responses=False)
    services = ApiServices.create(
        DatabaseClient.from_settings(
            DatabaseSettings(
                url="sqlite+pysqlite:///:memory:",
                application_name=DatabaseApplicationName.Test,
            )
        ),
        root=root,
        redis_client=redis_client,
        binary_redis_client=binary_redis_client,
        owns_redis_client=True,
        owns_binary_redis_client=True,
    )
    control = ControlPlaneService(services.context)
    owner = UserService(services.context).create(display_name="benchmark-owner")
    control.set_workspace("default", owner_user_id=owner.id)
    return services


class _BenchmarkObjectClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        target_bucket = bucket or "default"
        self.objects[(target_bucket, key)] = data
        return S3ObjectInfo(
            bucket=target_bucket,
            key=key,
            size=len(data),
            metadata=metadata or {},
        )

    def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        return self.put_bytes(
            key,
            Path(source).expanduser().resolve().read_bytes(),
            bucket=bucket,
            content_type=content_type,
            metadata=metadata,
        )

    def read_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        return self.objects[(bucket or "default", key)]

    def download_file(
        self,
        key: str,
        target: str | Path,
        *,
        bucket: str | None = None,
    ) -> S3ObjectInfo:
        data = self.read_bytes(key, bucket=bucket)
        Path(target).write_bytes(data)
        return S3ObjectInfo(bucket=bucket or "default", key=key, size=len(data))

    def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo:
        data = self.read_bytes(key, bucket=bucket)
        return S3ObjectInfo(
            bucket=bucket or "default",
            key=key,
            size=len(data),
        )

    def exists(self, key: str, *, bucket: str | None = None) -> bool:
        return (bucket or "default", key) in self.objects

    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        return f"https://objects.test/{bucket or 'default'}/{key}?expires={expires_seconds}"

    def generate_presigned_put_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int = 0,
        content_type: str = "application/octet-stream",
    ) -> str:
        _ = content_length, content_type
        return f"https://objects.test/{bucket or 'default'}/{key}?expires={expires_seconds}"

    def delete(self, key: str, *, bucket: str | None = None) -> None:
        self.objects.pop((bucket or "default", key), None)


def _measure(
    action: Callable[[], dict[str, JsonValue]],
) -> tuple[BenchmarkStatus, float, dict[str, JsonValue], str | None]:
    started = time.perf_counter()
    try:
        details = action()
        status = BenchmarkStatus.Passed
        error = None
    except Exception as exc:
        details = {}
        status = BenchmarkStatus.Failed
        error = f"{type(exc).__name__}: {exc}"
    duration_ms = (time.perf_counter() - started) * 1000
    return status, duration_ms, details, error


def run_case(
    kind: BenchmarkKind,
    *,
    workspace: Path | None = None,
    sandbox_config: SandboxParallelConfig | None = None,
) -> BenchmarkResult:
    case = case_for(kind)
    with ExitStack() as stack:
        root = workspace or Path(stack.enter_context(tempfile.TemporaryDirectory()))
        root.mkdir(parents=True, exist_ok=True)
        service_bundle: ApiServices | None = None
        stack.callback(lambda: service_bundle.close() if service_bundle is not None else None)

        def services() -> ApiServices:
            nonlocal service_bundle
            if service_bundle is None:
                service_bundle = _services(root)
            return service_bundle

        def startup() -> dict[str, JsonValue]:
            result = run_process([sys.executable, "-c", "print('ready')"], timeout_seconds=5)
            if not result.ok:
                msg = result.stderr or f"process exited with {result.exit_code}"
                raise RuntimeError(msg)
            return {"stdout": result.stdout.strip(), "exit_code": result.exit_code}

        def cache() -> dict[str, JsonValue]:
            service_bundle = services()
            source = root / "payload.txt"
            source.write_text("payload", encoding="utf-8")
            object_client = _BenchmarkObjectClient()
            object_record = ObjectStorage(
                service_bundle.context,
                object_client=object_client,
            ).put_file("bench", "payload.txt", source)
            if not object_client.exists("payload.txt", bucket="bench"):
                raise RuntimeError("benchmark object upload was not persisted")
            cache_record = CacheStorage(service_bundle.context).put("bench", "payload", source)
            return {
                "object_size": object_record.size,
                "cache_size": cache_record.size,
                "sha256": cache_record.sha256,
            }

        def sandbox() -> dict[str, JsonValue]:
            plan = build_sandbox_parallel_plan(
                sandbox_config or SandboxParallelConfig(count=1, parallelism=1, warmup=0)
            )
            payload = plan.model_dump(mode="json")
            return {
                "live_execution_required": payload["live_execution_required"],
                "command": payload["command"],
                "config": payload["config"],
                "note": payload["note"],
            }

        actions = {
            BenchmarkKind.Startup: startup,
            BenchmarkKind.Cache: cache,
            BenchmarkKind.Sandbox: sandbox,
        }
        status, duration_ms, details, error = _measure(actions[kind])
        return BenchmarkResult(
            case=case,
            status=status,
            duration_ms=duration_ms,
            metrics={"duration_ms": duration_ms},
            details=details,
            error=error,
        )


def run_suite(
    kinds: Iterable[BenchmarkKind] | None = None,
    *,
    workspace: Path | None = None,
) -> BenchmarkReport:
    selected = list(kinds or [item.kind for item in DEFAULT_CASES])
    return BenchmarkReport(results=[run_case(kind, workspace=workspace) for kind in selected])


def run_named_suite(
    name: str,
    *,
    workspace: Path | None = None,
    loader: SuiteLoader | None = None,
) -> BenchmarkReport:
    suite_loader = loader or SuiteLoader()
    results = _run_loaded_suite(name, workspace=workspace, loader=suite_loader, seen=frozenset())
    return BenchmarkReport(results=results)


def _run_loaded_suite(
    name: str,
    *,
    workspace: Path | None,
    loader: SuiteLoader,
    seen: frozenset[str],
) -> list[BenchmarkResult]:
    if name in seen:
        msg = f"benchmark suite includes itself recursively: {name}"
        raise ValueError(msg)
    suite = loader.load(name)
    if suite.includes:
        results: list[BenchmarkResult] = []
        next_seen = seen | {name}
        for included in suite.includes:
            results.extend(
                _run_loaded_suite(included, workspace=workspace, loader=loader, seen=next_seen)
            )
        return results
    result = run_case(
        _case_kind_for_suite(suite),
        workspace=workspace,
        sandbox_config=_sandbox_config_for_suite(suite)
        if suite.kind is BenchmarkKind.Sandbox
        else None,
    )
    details = dict(result.details)
    details.update(
        {
            "suite": suite.name,
            "runner": suite.runner.value,
            "scenarios": list(suite.scenario_names),
            "file_plan": suite.file_plan,
            "defaults": suite.defaults,
        }
    )
    return [result.model_copy(update={"details": details})]


def _case_kind_for_suite(suite: SuiteSpec) -> BenchmarkKind:
    if suite.kind in {BenchmarkKind.Startup, BenchmarkKind.Cache, BenchmarkKind.Sandbox}:
        return suite.kind
    if suite.kind == BenchmarkKind.Filesystem:
        return BenchmarkKind.Cache
    if suite.kind == BenchmarkKind.Image:
        return BenchmarkKind.Startup
    msg = f"suite {suite.name!r} must include concrete suites before it can run locally"
    raise ValueError(msg)


def _sandbox_config_for_suite(suite: SuiteSpec) -> SandboxParallelConfig:
    defaults = suite.defaults
    return SandboxParallelConfig(
        count=_int_default(defaults, "count", 10),
        parallelism=_int_default(defaults, "parallelism", 4),
        warmup=_int_default(defaults, "warmup", 1),
        prewarm_count=_int_default(defaults, "prewarm_count", 0),
        prewarm_parallelism=_int_default(defaults, "prewarm_parallelism", 0),
        image=_str_default(defaults, "image", "python:3.12-slim"),
        exec_command=_command_default(
            defaults,
            "exec",
            ("sh", "-lc", 'printf "%s\\n" "$BENCHMARK_EXPECTED_OUTPUT"'),
        ),
        expected_output_template=_str_default(
            defaults,
            "expected_output",
            "sandbox-ready-{index}",
        ),
        output_tail_bytes=_int_default(defaults, "output_tail_bytes", 4096),
        wait_running=_bool_default(defaults, "wait_running", True),
        wait_exec_complete=_bool_default(defaults, "wait_exec_complete", True),
        prepare_sandbox=_bool_default(defaults, "prepare_sandbox", True),
        run_live=_bool_default(defaults, "run_live", False),
    )


def _int_default(defaults: Mapping[str, JsonValue], key: str, fallback: int) -> int:
    value = defaults.get(key)
    return value if isinstance(value, int) else fallback


def _bool_default(defaults: Mapping[str, JsonValue], key: str, fallback: bool) -> bool:
    value = defaults.get(key)
    return value if isinstance(value, bool) else fallback


def _str_default(defaults: Mapping[str, JsonValue], key: str, fallback: str) -> str:
    value = defaults.get(key)
    return value if isinstance(value, str) and value else fallback


def _command_default(
    defaults: Mapping[str, JsonValue],
    key: str,
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    value = defaults.get(key)
    if isinstance(value, str) and value.strip():
        return tuple(shlex.split(value))
    if isinstance(value, list):
        command = tuple(item for item in value if isinstance(item, str))
        if len(command) == len(value):
            return command
    return fallback
