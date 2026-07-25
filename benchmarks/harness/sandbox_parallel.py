from __future__ import annotations

import json
import os
import shlex
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from lazycloud.abstractions.image import Image
from lazycloud.abstractions.sandbox import Sandbox, SandboxInstance, SandboxProcess
from pydantic import Field, SecretStr, field_validator, model_validator

from benchmarks.harness.models import BenchmarkModel
from benchmarks.harness.startup_report import (
    StartupPhaseId,
    StartupReport,
    StartupSample,
    build_startup_report,
    percentile,
    render_startup_markdown,
)

DEFAULT_SANDBOX_IMAGE = "python:3.12-slim"
DEFAULT_SANDBOX_NAME = "sandbox-parallel-benchmark"
DEFAULT_EXPECTED_OUTPUT_TEMPLATE = "sandbox-ready-{index}"
DEFAULT_EXEC_COMMAND = ("sh", "-lc", 'printf "%s\\n" "$BENCHMARK_EXPECTED_OUTPUT"')
DEFAULT_CONTAINER_COMMAND = ("tail", "-f", "/dev/null")
DEFAULT_OUTPUT_TAIL_BYTES = 4096


class SandboxParallelConfig(BenchmarkModel):
    count: int = 10
    parallelism: int = 4
    warmup: int = 1
    prewarm_count: int = 0
    prewarm_parallelism: int = 0
    endpoint: str | None = None
    workspace: str | None = None
    token: SecretStr | None = Field(default=None, exclude=True)
    sandbox_name: str = DEFAULT_SANDBOX_NAME
    image: str = DEFAULT_SANDBOX_IMAGE
    image_id: str = ""
    container_command: tuple[str, ...] = DEFAULT_CONTAINER_COMMAND
    exec_command: tuple[str, ...] = DEFAULT_EXEC_COMMAND
    exec_cwd: str = "/workspace"
    expected_output_template: str = DEFAULT_EXPECTED_OUTPUT_TEMPLATE
    output_tail_bytes: int = DEFAULT_OUTPUT_TAIL_BYTES
    cpu: float | None = 0.1
    memory: str | None = "256Mi"
    gpu: str | None = None
    gpu_count: int = 0
    keep_warm_seconds: int = 600
    cleanup_ttl_seconds: int = 5
    request_timeout_seconds: float = 10.0
    ready_timeout_seconds: float = 120.0
    create_retries: int = 3
    retry_interval_seconds: float = 0.25
    prepare_sandbox: bool = True
    wait_running: bool = True
    wait_exec_complete: bool = True
    keep_sandboxes: bool = False
    run_live: bool = False
    output: Path | None = None
    report: Path | None = None

    @field_validator("count", "parallelism")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value <= 0:
            msg = "sandbox parallel count and parallelism must be greater than zero"
            raise ValueError(msg)
        return value

    @field_validator("warmup", "prewarm_count", "prewarm_parallelism", "create_retries")
    @classmethod
    def _non_negative_int(cls, value: int) -> int:
        if value < 0:
            msg = "sandbox parallel count values cannot be negative"
            raise ValueError(msg)
        return value

    @field_validator("gpu_count", "keep_warm_seconds", "cleanup_ttl_seconds", "output_tail_bytes")
    @classmethod
    def _non_negative_resource_int(cls, value: int) -> int:
        if value < 0:
            msg = "sandbox parallel resource values cannot be negative"
            raise ValueError(msg)
        return value

    @field_validator("request_timeout_seconds", "ready_timeout_seconds", "retry_interval_seconds")
    @classmethod
    def _positive_float(cls, value: float) -> float:
        if value <= 0:
            msg = "sandbox parallel timeout values must be greater than zero"
            raise ValueError(msg)
        return value

    @field_validator("container_command", "exec_command")
    @classmethod
    def _command_is_not_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            msg = "sandbox parallel commands cannot be empty"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _fill_prewarm_parallelism(self) -> Self:
        if self.prewarm_count > 0 and self.prewarm_parallelism == 0:
            self.prewarm_parallelism = min(self.parallelism, self.prewarm_count)
        if self.prewarm_count > 0 and self.prewarm_parallelism <= 0:
            msg = "prewarm_parallelism must be greater than zero when prewarming"
            raise ValueError(msg)
        if not self.image and not self.image_id:
            msg = "either image or image_id must be provided"
            raise ValueError(msg)
        return self

    @property
    def token_value(self) -> str | None:
        return self.token.get_secret_value() if self.token is not None else None


class SandboxExecVerification(BenchmarkModel):
    verified: bool
    output_matched: bool | None = None
    error: str | None = None


class SandboxParallelSample(StartupSample):
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    stub_id: str = ""
    create_attempts: int = 0
    create_errors: tuple[str, ...] = ()
    running_attempts: int = 0
    exec_attempts: int = 0
    exec_errors: tuple[str, ...] = ()
    exec_pid: int | None = None
    exec_expected_output: str = ""
    exec_output_matched: bool | None = None
    exec_stdout: str = ""
    exec_stderr: str = ""
    exec_stdout_bytes: int = 0
    exec_stderr_bytes: int = 0
    exec_stdout_truncated: bool = False
    exec_stderr_truncated: bool = False
    cleanup_error: str | None = None
    terminated: bool = False


class SandboxParallelBatch(BenchmarkModel):
    count: int
    ok_count: int
    wall_ms: float
    throughput_per_second: float


class SandboxParallelPlan(BenchmarkModel):
    config: SandboxParallelConfig
    live_execution_required: bool = True
    command: str
    note: str = (
        "This is a deterministic benchmark plan. Pass --run-live against a configured "
        "control plane to create sandboxes and collect timings."
    )

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"

    def to_markdown(self) -> str:
        lines = [
            "# Sandbox Parallel Benchmark Plan",
            "",
            f"- Live execution required: `{str(self.live_execution_required).lower()}`",
            f"- Count: `{self.config.count}`",
            f"- Parallelism: `{self.config.parallelism}`",
            f"- Warmup: `{self.config.warmup}`",
            f"- Prewarm: `{self.config.prewarm_count}`",
            f"- Image: `{self.config.image_id or self.config.image}`",
            f"- Exec: `{self.command}`",
            f"- Expected output: `{self.config.expected_output_template}`",
            "",
            self.note,
            "",
        ]
        return "\n".join(lines)


class SandboxParallelResult(BenchmarkModel):
    config: SandboxParallelConfig
    samples: tuple[SandboxParallelSample, ...]
    startup_report: StartupReport
    batch: SandboxParallelBatch
    duration_ms: float
    started_at: datetime
    finished_at: datetime

    @property
    def measured_samples(self) -> tuple[SandboxParallelSample, ...]:
        return tuple(sample for sample in self.samples if not sample.warmup)

    @property
    def failed_samples(self) -> tuple[SandboxParallelSample, ...]:
        return tuple(sample for sample in self.measured_samples if not sample.ok)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"

    def to_markdown(self) -> str:
        lines = [
            "# Sandbox Parallel Benchmark",
            "",
            f"- Count: `{self.config.count}`",
            f"- Parallelism: `{self.config.parallelism}`",
            f"- Duration: `{self.duration_ms:.2f}ms`",
            f"- Measured OK: `{self.batch.ok_count}/{self.batch.count}`",
            f"- Throughput: `{self.batch.throughput_per_second:.2f}/s`",
            "",
            (
                "| Run | Warmup | Sandbox | Create ms | Running ms | Ready ms | "
                "Exec ms | Check | Status |"
            ),
            "| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
        ]
        for sample in sorted(self.samples, key=lambda item: item.index):
            lines.append(
                "| "
                f"{sample.index} | `{str(sample.warmup).lower()}` | "
                f"{sample.sandbox_id or ''} | "
                f"{_format_optional_ms(sample.accepted_ms)} | "
                f"{_format_optional_ms(sample.running_observed_ms)} | "
                f"{_format_optional_ms(sample.process_ready_ms)} | "
                f"{_format_optional_ms(sample.exec_complete_ms)} | "
                f"`{_sample_check_label(sample)}` | "
                f"`{'ok' if sample.ok else 'failed'}` |"
            )
        lines.extend(["", render_startup_markdown(self.startup_report)])
        return "\n".join(lines)


def config_from_env() -> SandboxParallelConfig:
    return SandboxParallelConfig(
        count=_env_int("BENCHMARK_SANDBOX_COUNT", 10),
        parallelism=_env_int("BENCHMARK_SANDBOX_PARALLELISM", 4),
        warmup=_env_int("BENCHMARK_SANDBOX_WARMUP", 1),
        prewarm_count=_env_int("BENCHMARK_SANDBOX_PREWARM_COUNT", 0),
        prewarm_parallelism=_env_int("BENCHMARK_SANDBOX_PREWARM_PARALLELISM", 0),
        endpoint=os.getenv("BENCHMARK_ENDPOINT") or None,
        workspace=os.getenv("BENCHMARK_WORKSPACE") or None,
        token=_secret_from_env("BENCHMARK_TOKEN"),
        sandbox_name=os.getenv("BENCHMARK_SANDBOX_NAME", DEFAULT_SANDBOX_NAME),
        image=os.getenv("BENCHMARK_SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE),
        image_id=os.getenv("BENCHMARK_SANDBOX_IMAGE_ID", ""),
        container_command=_split_command_env(
            "BENCHMARK_SANDBOX_CONTAINER_COMMAND",
            DEFAULT_CONTAINER_COMMAND,
        ),
        exec_command=_split_command_env("BENCHMARK_SANDBOX_EXEC", DEFAULT_EXEC_COMMAND),
        exec_cwd=os.getenv("BENCHMARK_SANDBOX_EXEC_CWD", "/workspace"),
        expected_output_template=os.getenv(
            "BENCHMARK_SANDBOX_EXPECTED_OUTPUT",
            DEFAULT_EXPECTED_OUTPUT_TEMPLATE,
        ),
        output_tail_bytes=_env_int("BENCHMARK_SANDBOX_OUTPUT_TAIL_BYTES", 4096),
        cpu=_env_float_or_none("BENCHMARK_SANDBOX_CPU", 0.1),
        memory=os.getenv("BENCHMARK_SANDBOX_MEMORY", "256Mi") or None,
        gpu=os.getenv("BENCHMARK_SANDBOX_GPU") or None,
        gpu_count=_env_int("BENCHMARK_SANDBOX_GPU_COUNT", 0),
        keep_warm_seconds=_env_int("BENCHMARK_SANDBOX_KEEP_WARM_SECONDS", 600),
        cleanup_ttl_seconds=_env_int("BENCHMARK_SANDBOX_CLEANUP_TTL_SECONDS", 5),
        request_timeout_seconds=_env_float("BENCHMARK_REQUEST_TIMEOUT_SECONDS", 10.0),
        ready_timeout_seconds=_env_float("BENCHMARK_SANDBOX_READY_TIMEOUT_SECONDS", 120.0),
        create_retries=_env_int("BENCHMARK_SANDBOX_CREATE_RETRIES", 3),
        retry_interval_seconds=_env_float("BENCHMARK_SANDBOX_RETRY_INTERVAL_SECONDS", 0.25),
        prepare_sandbox=_env_bool("BENCHMARK_SANDBOX_PREPARE", True),
        wait_running=_env_bool("BENCHMARK_SANDBOX_WAIT_RUNNING", True),
        wait_exec_complete=_env_bool("BENCHMARK_SANDBOX_WAIT_EXEC_COMPLETE", True),
        keep_sandboxes=_env_bool("BENCHMARK_SANDBOX_KEEP", False),
        run_live=_env_bool("BENCHMARK_SANDBOX_RUN_LIVE", False),
        output=_env_path("BENCHMARK_SANDBOX_OUTPUT"),
        report=_env_path("BENCHMARK_SANDBOX_REPORT"),
    )


def build_sandbox_parallel_plan(config: SandboxParallelConfig | None = None) -> SandboxParallelPlan:
    selected = config or config_from_env()
    return SandboxParallelPlan(config=selected, command=shlex.join(selected.exec_command))


def run_sandbox_parallel(
    config: SandboxParallelConfig | None = None,
    *,
    workspace: Path | None = None,
) -> SandboxParallelResult:
    selected = config or config_from_env()
    if not selected.run_live:
        msg = "sandbox parallel benchmark requires run_live=True or the --run-live CLI flag"
        raise RuntimeError(msg)

    started_at = datetime.now(UTC)
    started = time.perf_counter()
    if workspace is not None:
        workspace.mkdir(parents=True, exist_ok=True)
    sandbox = _build_sdk_sandbox(selected)
    if selected.prepare_sandbox:
        sandbox.prepare(workspace=selected.workspace)

    samples: list[SandboxParallelSample] = []
    next_index = 1
    if selected.prewarm_count:
        prewarm_samples, _ = _run_parallel_batch(
            selected,
            sandbox,
            count=selected.prewarm_count,
            parallelism=selected.prewarm_parallelism,
            start_index=next_index,
            warmup=True,
        )
        samples.extend(prewarm_samples)
        next_index += selected.prewarm_count

    if selected.warmup:
        warmup_samples, _ = _run_parallel_batch(
            selected,
            sandbox,
            count=selected.warmup,
            parallelism=min(selected.parallelism, selected.warmup),
            start_index=next_index,
            warmup=True,
        )
        samples.extend(warmup_samples)
        next_index += selected.warmup

    measured_samples, batch = _run_parallel_batch(
        selected,
        sandbox,
        count=selected.count,
        parallelism=selected.parallelism,
        start_index=next_index,
        warmup=False,
    )
    samples.extend(measured_samples)
    samples.sort(key=lambda item: item.index)
    startup_report = build_startup_report(samples)
    finished_at = datetime.now(UTC)
    result = SandboxParallelResult(
        config=selected,
        samples=tuple(samples),
        startup_report=startup_report,
        batch=batch,
        duration_ms=(time.perf_counter() - started) * 1000,
        started_at=started_at,
        finished_at=finished_at,
    )
    write_sandbox_parallel_outputs(result)
    if result.failed_samples:
        msg = f"{len(result.failed_samples)} measured sandbox benchmark run(s) failed"
        raise RuntimeError(msg)
    return result


def write_sandbox_parallel_outputs(result: SandboxParallelResult | SandboxParallelPlan) -> None:
    output = result.config.output
    report = result.config.report
    if output is not None:
        _write_text(output, result.to_json())
    if report is not None:
        _write_text(report, result.to_markdown())


def expected_exec_output(
    config: SandboxParallelConfig,
    sample: SandboxParallelSample,
) -> str:
    try:
        return config.expected_output_template.format(
            index=sample.index,
            sandbox_id=sample.sandbox_id or "",
            container_id=sample.sandbox_id or "",
            stub_id=sample.stub_id,
            warmup=str(sample.warmup).lower(),
        )
    except (IndexError, KeyError, ValueError) as exc:
        msg = f"invalid sandbox expected output template {config.expected_output_template!r}: {exc}"
        raise ValueError(msg) from exc


def text_tail(value: str, max_bytes: int) -> tuple[str, bool, int]:
    encoded = value.encode("utf-8", errors="replace")
    if max_bytes <= 0 or len(encoded) <= max_bytes:
        return value, False, len(encoded)
    return encoded[-max_bytes:].decode("utf-8", errors="replace"), True, len(encoded)


def verify_exec_output(
    *,
    exit_code: int | None,
    stdout: str,
    expected_output: str,
) -> SandboxExecVerification:
    if exit_code is None:
        return SandboxExecVerification(
            verified=False,
            output_matched=None,
            error="exec completion was not observed",
        )
    if exit_code != 0:
        matched = None if not expected_output else stdout.rstrip("\r\n") == expected_output
        return SandboxExecVerification(
            verified=False,
            output_matched=matched,
            error=f"exit code {exit_code}",
        )
    if not expected_output:
        return SandboxExecVerification(verified=True, output_matched=None)
    actual = stdout.rstrip("\r\n")
    matched = actual == expected_output
    return SandboxExecVerification(
        verified=matched,
        output_matched=matched,
        error=None if matched else f"stdout mismatch: expected {expected_output!r}, got {actual!r}",
    )


def summarize_exec_verification(samples: Iterable[SandboxParallelSample]) -> dict[str, int]:
    measured = [sample for sample in samples if not sample.warmup]
    completed = [sample for sample in measured if sample.exec_exit_code is not None]
    verified = [sample for sample in measured if sample.exec_verified]
    mismatched = [sample for sample in measured if sample.exec_output_matched is False]
    return {
        "count": len(measured),
        "completed": len(completed),
        "verified": len(verified),
        "mismatched": len(mismatched),
        "failed": len(measured) - len(verified),
    }


def _build_sdk_sandbox(config: SandboxParallelConfig) -> Sandbox:
    image = Image.from_id(config.image_id) if config.image_id else Image.from_registry(config.image)
    sandbox = Sandbox(
        _app_slug=config.sandbox_name,
        name=config.sandbox_name,
        image=image,
        command=config.container_command,
        cpu=config.cpu if config.cpu is not None else 1.0,
        memory=config.memory if config.memory is not None else 128,
        gpu=config.gpu,
        gpu_count=config.gpu_count,
        keep_warm_seconds=config.keep_warm_seconds,
        authorized=False,
        sync_local_dir=False,
    )
    sandbox.endpoint = config.endpoint
    sandbox.token = config.token_value
    sandbox.workspace = config.workspace
    sandbox.timeout_seconds = config.request_timeout_seconds
    return sandbox


def _run_parallel_batch(
    config: SandboxParallelConfig,
    sandbox: Sandbox,
    *,
    count: int,
    parallelism: int,
    start_index: int,
    warmup: bool,
) -> tuple[list[SandboxParallelSample], SandboxParallelBatch]:
    batch_started = time.perf_counter()
    start_event = threading.Event()
    samples: list[SandboxParallelSample] = []
    with ThreadPoolExecutor(max_workers=min(parallelism, count)) as executor:
        futures = [
            executor.submit(
                _run_one_sample,
                config,
                sandbox,
                start_index + offset,
                warmup,
                start_event,
            )
            for offset in range(count)
        ]
        start_event.set()
        for future in as_completed(futures):
            samples.append(future.result())
    wall_ms = (time.perf_counter() - batch_started) * 1000
    ok_count = sum(1 for sample in samples if sample.ok)
    return samples, SandboxParallelBatch(
        count=count,
        ok_count=ok_count,
        wall_ms=wall_ms,
        throughput_per_second=ok_count / (wall_ms / 1000) if wall_ms > 0 else 0,
    )


def _run_one_sample(
    config: SandboxParallelConfig,
    sandbox: Sandbox,
    index: int,
    warmup: bool,
    start_event: threading.Event,
) -> SandboxParallelSample:
    start_event.wait()
    started = time.perf_counter()
    sample = SandboxParallelSample(index=index, warmup=warmup)
    instance: SandboxInstance | None = None
    try:
        instance, attempts, errors = _create_with_retries(config, sandbox)
        if instance is None:
            raise RuntimeError("sandbox create returned no instance")
        sample.accepted_ms = _elapsed_ms(started)
        sample.create_attempts = attempts
        sample.create_errors = tuple(errors)
        sample.sandbox_id = str(instance.container_id)
        sample.stub_id = str(instance.stub_id)
        if config.wait_running:
            running_ms, running_attempts = _wait_for_process_manager(config, instance, started)
            sample.running_observed_ms = running_ms
            sample.running_attempts = running_attempts
        else:
            sample.running_observed_ms = sample.accepted_ms

        _exec_readiness(config, instance, sample, started)
        sample.ok = bool(sample.exec_pid) if not config.wait_exec_complete else sample.exec_verified
        if not sample.ok and not sample.error:
            sample.error = "sandbox readiness command was not verified"
    except Exception as exc:
        sample.ok = False
        sample.error = f"{type(exc).__name__}: {exc}"
        if sample.exec_complete_ms is None:
            sample.exec_complete_ms = _elapsed_ms(started)
    finally:
        _cleanup_sample(config, instance, sample)
        sample.phase_durations_ms = _phase_durations(sample)
        sample.finished_at = datetime.now(UTC)
    return sample


def _create_with_retries(
    config: SandboxParallelConfig,
    sandbox: Sandbox,
) -> tuple[SandboxInstance, int, list[str]]:
    errors: list[str] = []
    for attempt in range(1, config.create_retries + 2):
        try:
            return sandbox.create(), attempt, errors
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt > config.create_retries:
                raise
            time.sleep(config.retry_interval_seconds * min(attempt, 8))
    raise RuntimeError("sandbox create retry loop exited unexpectedly")


def _wait_for_process_manager(
    config: SandboxParallelConfig,
    instance: SandboxInstance,
    started: float,
) -> tuple[float, int]:
    deadline = time.perf_counter() + config.ready_timeout_seconds
    attempts = 0
    last_error = ""
    while time.perf_counter() < deadline:
        attempts += 1
        try:
            instance.list_processes()
            return _elapsed_ms(started), attempts
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(config.retry_interval_seconds)
    msg = f"timed out waiting for sandbox process manager readiness; last error: {last_error}"
    raise TimeoutError(msg)


def _exec_readiness(
    config: SandboxParallelConfig,
    instance: SandboxInstance,
    sample: SandboxParallelSample,
    started: float,
) -> None:
    deadline = time.perf_counter() + config.ready_timeout_seconds
    errors: list[str] = []
    sample.exec_expected_output = expected_exec_output(config, sample)
    env = {"BENCHMARK_EXPECTED_OUTPUT": sample.exec_expected_output}
    while time.perf_counter() < deadline:
        sample.exec_attempts += 1
        process: SandboxProcess | None = None
        try:
            process = instance.process.exec(*config.exec_command, cwd=config.exec_cwd, env=env)
            if process is None:
                raise RuntimeError("sandbox exec returned no process")
            sample.process_ready_ms = _elapsed_ms(started)
            sample.exec_pid = int(process.pid)
            if not config.wait_exec_complete:
                sample.exec_verified = False
                return
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError("sandbox readiness command did not finish before timeout")
            sample.exec_exit_code = int(process.wait(timeout=remaining))
            sample.exec_complete_ms = _elapsed_ms(started)
            stdout = str(process.stdout.read())
            stderr = str(process.stderr.read())
            (
                sample.exec_stdout,
                sample.exec_stdout_truncated,
                sample.exec_stdout_bytes,
            ) = text_tail(stdout, config.output_tail_bytes)
            (
                sample.exec_stderr,
                sample.exec_stderr_truncated,
                sample.exec_stderr_bytes,
            ) = text_tail(stderr, config.output_tail_bytes)
            verification = verify_exec_output(
                exit_code=sample.exec_exit_code,
                stdout=stdout,
                expected_output=sample.exec_expected_output,
            )
            sample.exec_verified = verification.verified
            sample.exec_output_matched = verification.output_matched
            sample.error = verification.error
            return
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            sample.exec_errors = tuple(errors)
            if process is not None:
                _kill_process(process)
            if time.perf_counter() >= deadline:
                raise
            time.sleep(config.retry_interval_seconds)
    raise TimeoutError("timed out waiting for sandbox readiness command")


def _cleanup_sample(
    config: SandboxParallelConfig,
    instance: SandboxInstance | None,
    sample: SandboxParallelSample,
) -> None:
    if instance is None or config.keep_sandboxes:
        return
    errors: list[str] = []
    if config.cleanup_ttl_seconds:
        try:
            instance.update_ttl(config.cleanup_ttl_seconds)
        except Exception as exc:
            errors.append(f"update_ttl: {type(exc).__name__}: {exc}")
    try:
        sample.terminated = bool(instance.terminate())
    except Exception as exc:
        errors.append(f"terminate: {type(exc).__name__}: {exc}")
    if errors:
        sample.cleanup_error = "; ".join(errors)


def _kill_process(process: SandboxProcess) -> None:
    try:
        process.kill()
    except Exception:
        return


def _phase_durations(sample: SandboxParallelSample) -> dict[StartupPhaseId, float]:
    accepted = sample.accepted_ms or 0
    running = sample.running_observed_ms or accepted
    ready = sample.process_ready_ms or running
    complete = sample.exec_complete_ms or ready
    return {
        StartupPhaseId.Scheduler: accepted,
        StartupPhaseId.WorkerReceiveToRunning: max(running - accepted, 0),
        StartupPhaseId.ImageLoad: 0,
        StartupPhaseId.ProcessStartup: max(ready - running, 0),
        StartupPhaseId.ProcessManagerReady: max(ready - running, 0),
        StartupPhaseId.RunnerExecution: max(complete - ready, 0),
        StartupPhaseId.ResultDelivery: max(complete - running, 0),
    }


def _sample_check_label(sample: SandboxParallelSample) -> str:
    if sample.exec_verified:
        return "verified"
    if sample.exec_exit_code is not None:
        return "mismatch" if sample.exec_output_matched is False else "exit"
    if sample.exec_pid:
        return "accepted"
    return "missing"


def _format_optional_ms(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value in (None, "") else int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value in (None, "") else float(value)


def _env_float_or_none(name: str, default: float | None) -> float | None:
    value = os.getenv(name)
    if value is None:
        return default
    if value == "":
        return None
    return float(value)


def _env_path(name: str) -> Path | None:
    value = os.getenv(name)
    return Path(value).expanduser() if value else None


def _secret_from_env(name: str) -> SecretStr | None:
    value = os.getenv(name)
    return SecretStr(value) if value else None


def _split_command_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    return default if not value else tuple(shlex.split(value))


def summarize_metric(samples: Iterable[SandboxParallelSample], key: str) -> dict[str, float] | None:
    values = [
        float(value)
        for sample in samples
        if not sample.warmup and (value := getattr(sample, key)) is not None and sample.ok
    ]
    if not values:
        return None
    return {
        "count": float(len(values)),
        "min": min(values),
        "p50": percentile(values, 50) or 0,
        "p90": percentile(values, 90) or 0,
        "p95": percentile(values, 95) or 0,
        "max": max(values),
    }
