"""Container dispatch-latency benchmark.

Deploys a trivial function through the public SDK path against the root Compose
stack and measures, per invocation, where the wall time goes between task
submission and completion. The phase breakdown is taken from server-provided
task timestamps (``created_at``/``started_at``/``finished_at``) and the
container event-summary lifecycle durations, never from host wall-clock deltas,
so the numbers are immune to Docker Desktop VM clock drift. Host wall time is
reported only as an approximate round-trip and labelled as such.

Three one-shot function scenarios are measured over ``runs`` deploy cycles:

- ``cold``: first invocation of a freshly deployed function (no warm container).
- ``back-to-back``: an immediate second invocation using a new function
  container while the worker, image, and source caches are warm.
- ``idle-fresh``: another new function container after a fixed idle interval,
  with the image and source archive still cached.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from lazycloud.abstractions.function import Function
from pydantic import Field, JsonValue, SecretStr, TypeAdapter, ValidationError, field_validator
from shared.containers import ContainerStatus
from shared.env import GATEWAY_HTTP_URL_ENV, GATEWAY_TOKEN_ENV, WORKSPACE_ID_ENV
from shared.http.compute import ContainerDetailResponse
from shared.http.system import TokenCreateResponse
from shared.http.tasks import TaskResponse
from shared.http.workspaces import WorkspaceCreateRequest, WorkspaceResponse
from shared.tasks import TaskStatus

from benchmarks.harness.models import BenchmarkModel
from benchmarks.harness.startup_report import LatencySummary, summarize_values

DEFAULT_ENDPOINT = "http://127.0.0.1:8000"
DEFAULT_COMPOSE_CLI = ("docker", "compose")
DEFAULT_RUNS = 8
DEFAULT_INVOKE_TIMEOUT_SECONDS = 180.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.2
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_IDLE_SECONDS = 13.0

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class LatencyScenario(StrEnum):
    Cold = "cold"
    BackToBack = "back-to-back"
    IdleFresh = "idle-fresh"


# Phase keys are plain strings so the container event-summary lifecycle metrics
# are captured exactly as the server emits them (for example ``load_image_ms``,
# ``setup_network_ms``, ``run_runtime_ms``) rather than forced into a fixed set
# that could silently drop phases the pipeline actually reports. Three keys are
# derived here rather than taken from the event summary: the task-record legs and
# the host round-trip.
LEG_CREATED_TO_STARTED = "created_to_started"
LEG_CREATED_TO_CONTAINER_STARTED = "created_to_container_started"
LEG_CONTAINER_STARTED_TO_TASK_STARTED = "container_started_to_task_started"
LEG_STARTED_TO_FINISHED = "started_to_finished"
LEG_CREATED_TO_FINISHED = "created_to_finished"
HOST_ROUND_TRIP = "round_trip_host"

# Task-record legs are reported first and in this order; the host round-trip is
# always reported last. Everything between is an event-summary lifecycle metric.
TASK_LEG_ORDER: tuple[str, ...] = (
    LEG_CREATED_TO_STARTED,
    LEG_CREATED_TO_CONTAINER_STARTED,
    LEG_CONTAINER_STARTED_TO_TASK_STARTED,
    LEG_STARTED_TO_FINISHED,
    LEG_CREATED_TO_FINISHED,
)

PHASE_LABELS: dict[str, str] = {
    LEG_CREATED_TO_STARTED: "created -> started (queue+dispatch+start) [task record]",
    LEG_CREATED_TO_CONTAINER_STARTED: (
        "created -> container running (dispatch+OCI) [server records]"
    ),
    LEG_CONTAINER_STARTED_TO_TASK_STARTED: (
        "container running -> task started (launcher+runner boot) [server records]"
    ),
    LEG_STARTED_TO_FINISHED: "started -> finished (execution) [task record]",
    LEG_CREATED_TO_FINISHED: "created -> finished (server total) [task record]",
    HOST_ROUND_TRIP: "round-trip (host wall, approx)",
    "scheduler_ms": "scheduler (created -> claimed dispatch)",
    "load_image_ms": "image load",
    "setup_network_ms": "network setup",
    "prepare_runtime_ms": "runtime prepare",
    "run_runtime_ms": "runtime run / runner execution",
    "setup_mounts_ms": "mount setup",
    "build_spec_ms": "build runtime spec",
    "mark_running_ms": "mark running",
}


def phase_label(key: str) -> str:
    if key in PHASE_LABELS:
        return PHASE_LABELS[key]
    return key.removesuffix("_ms").replace("_", " ").replace("-", " ").strip()


class LatencyConfig(BenchmarkModel):
    runs: int = DEFAULT_RUNS
    endpoint: str = DEFAULT_ENDPOINT
    admin_token: SecretStr | None = Field(default=None, exclude=True)
    compose_cli: tuple[str, ...] = DEFAULT_COMPOSE_CLI
    cpu: float = 0.25
    memory: str = "128Mi"
    idle_seconds: float = DEFAULT_IDLE_SECONDS
    invoke_timeout_seconds: float = DEFAULT_INVOKE_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    run_live: bool = False
    output: Path | None = None

    @field_validator("runs")
    @classmethod
    def _positive_runs(cls, value: int) -> int:
        if value <= 0:
            msg = "latency benchmark runs must be greater than zero"
            raise ValueError(msg)
        return value

    @field_validator(
        "invoke_timeout_seconds",
        "idle_seconds",
        "poll_interval_seconds",
        "request_timeout_seconds",
    )
    @classmethod
    def _positive_float(cls, value: float) -> float:
        if value <= 0:
            msg = "latency benchmark timing values must be greater than zero"
            raise ValueError(msg)
        return value

    @property
    def admin_token_value(self) -> str | None:
        return self.admin_token.get_secret_value() if self.admin_token is not None else None


class LatencyPhaseSample(BenchmarkModel):
    scenario: LatencyScenario
    cycle: int
    task_id: str
    container_id: str
    status: TaskStatus
    exit_code: int | None = None
    ok: bool = True
    same_container_as_cold: bool | None = None
    created_at: datetime | None = None
    container_started_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    phase_ms: dict[str, float] = Field(default_factory=dict)
    error: str | None = None


class LatencyScenarioReport(BenchmarkModel):
    scenario: LatencyScenario
    samples: int
    ok_samples: int
    phase_order: tuple[str, ...] = ()
    phases: dict[str, LatencySummary] = Field(default_factory=dict)


class LatencyReport(BenchmarkModel):
    config: LatencyConfig
    one_shot_container_contract_confirmed: bool
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    workspace_id: str
    scenarios: tuple[LatencyScenarioReport, ...]
    all_samples: tuple[LatencyPhaseSample, ...]
    cleanup: tuple[str, ...] = ()
    cleanup_errors: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"

    def to_markdown(self) -> str:
        lines = [
            "# Dispatch Latency Benchmark",
            "",
            f"- Endpoint: `{self.config.endpoint}`",
            f"- Runs (deploy cycles): `{self.config.runs}`",
            f"- Idle interval: `{self.config.idle_seconds:.1f}s`",
            "- One-shot function containers confirmed: "
            f"`{str(self.one_shot_container_contract_confirmed).lower()}`",
            f"- Duration: `{self.duration_ms / 1000:.1f}s`",
            f"- Workspace: `{self.workspace_id}`",
            "",
            "Phase source: task/container record legs use server timestamps; lifecycle "
            "phases use container event-summary durations. `round-trip (host wall)` is "
            "approximate.",
            "",
        ]
        for scenario in self.scenarios:
            lines.extend(_scenario_markdown(scenario))
        lines.append("## Cleanup")
        lines.append("")
        for item in self.cleanup:
            lines.append(f"- {item}")
        if self.cleanup_errors:
            lines.append("")
            lines.append("### Cleanup errors")
            lines.append("")
            for item in self.cleanup_errors:
                lines.append(f"- {item}")
        lines.append("")
        return "\n".join(lines)


def _scenario_markdown(scenario: LatencyScenarioReport) -> list[str]:
    lines = [
        f"## {scenario.scenario.value}",
        "",
        f"- Samples: `{scenario.ok_samples}/{scenario.samples}` ok",
        "",
        "| Phase | Count | min | p50 (median) | p90 | max |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for phase in scenario.phase_order:
        summary = scenario.phases.get(phase)
        if summary is None:
            continue
        lines.append(
            f"| {phase_label(phase)} | {summary.count} | {summary.min:.1f} | "
            f"{summary.p50:.1f} | {summary.p90:.1f} | {summary.max:.1f} |"
        )
    lines.append("")
    return lines


def _ordered_phase_keys(present: set[str]) -> tuple[str, ...]:
    """Task-record legs first, then event-summary lifecycle phases, host last.

    Event phases are ordered by their median position would be ideal, but the
    summary keys have no inherent order, so they are sorted lexically for a
    stable table. The three task legs and the host round-trip are pinned.
    """
    legs = [key for key in TASK_LEG_ORDER if key in present]
    host = [HOST_ROUND_TRIP] if HOST_ROUND_TRIP in present else []
    events = sorted(present - set(TASK_LEG_ORDER) - {HOST_ROUND_TRIP})
    return tuple([*legs, *events, *host])


class LatencyBenchmarkError(RuntimeError):
    pass


class LatencyBenchmark:
    """Owns one live latency benchmark run and its cleanup lifecycle."""

    def __init__(self, config: LatencyConfig) -> None:
        self.config = config
        self.run_id = uuid.uuid4().hex[:10]
        self.workspace: WorkspaceResponse | None = None
        self.workspace_token: str = ""
        self.minted_admin_token: str = ""
        self.minted_admin_token_id: str = ""
        self.cleanup_notes: list[str] = []
        self.cleanup_errors: list[str] = []

    # -- lifecycle -----------------------------------------------------------

    def run(self) -> LatencyReport:
        if not self.config.run_live:
            msg = "latency benchmark requires run_live=True or the --run-live CLI flag"
            raise LatencyBenchmarkError(msg)
        started_at = datetime.now().astimezone()
        started = time.perf_counter()
        samples: list[LatencyPhaseSample] = []
        primary_error: BaseException | None = None
        try:
            self._resolve_admin_token()
            self._create_workspace()
            for cycle in range(self.config.runs):
                samples.extend(self._run_cycle(cycle))
        except BaseException as exc:
            primary_error = exc
        finally:
            self._cleanup()
        if primary_error is not None:
            raise primary_error
        finished_at = datetime.now().astimezone()
        return LatencyReport(
            config=self.config,
            one_shot_container_contract_confirmed=all(
                sample.same_container_as_cold is not True for sample in samples
            ),
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=(time.perf_counter() - started) * 1000,
            workspace_id=self._workspace.id,
            scenarios=tuple(_summarize_scenarios(samples)),
            all_samples=tuple(samples),
            cleanup=tuple(self.cleanup_notes),
            cleanup_errors=tuple(self.cleanup_errors),
        )

    def _run_cycle(self, cycle: int) -> list[LatencyPhaseSample]:
        function = self._deploy_function(cycle)
        cold = self._invoke(function, LatencyScenario.Cold, cycle)
        back_to_back = self._invoke(function, LatencyScenario.BackToBack, cycle)
        back_to_back.same_container_as_cold = back_to_back.container_id == cold.container_id
        time.sleep(self.config.idle_seconds)
        idle_fresh = self._invoke(function, LatencyScenario.IdleFresh, cycle)
        idle_fresh.same_container_as_cold = idle_fresh.container_id == cold.container_id
        return [cold, back_to_back, idle_fresh]

    # -- deploy + invoke -----------------------------------------------------

    def _deploy_function(self, cycle: int) -> Function[[int], int]:
        from benchmarks.harness import latency_workload
        from lazycloud import App, Image

        app_slug = f"lat_{self.run_id}_{cycle}"
        app = App(app_slug)
        function = app.function(
            latency_workload.noop,
            name="noop",
            image=Image(python_version="3.12"),
            cpu=self.config.cpu,
            memory=self.config.memory,
        )
        function.endpoint = self.config.endpoint
        function.token = self.workspace_token
        function.timeout = self.config.request_timeout_seconds
        self._configure_sdk_environment()
        self._deploy_scoped(function)
        return function

    def _deploy_scoped(self, function: Function[[int], int]) -> None:
        from lazycloud.session.deployment import DeploymentClient

        harness_dir = Path(__file__).resolve().parent
        client = DeploymentClient(
            workspace=self._workspace.id,
            endpoint=self.config.endpoint,
            token=self.workspace_token,
            timeout_seconds=self.config.invoke_timeout_seconds,
            sync_source=True,
            source_root=harness_dir,
            source_include_patterns=("latency_workload.py",),
        )
        response = client.create(
            function.spec(),
            name=function.resource_name,
            workspace=self._workspace.id,
            image=function.image,
        )
        function.stub_id = response.stub_id or function.stub_id
        if not function.stub_id:
            raise LatencyBenchmarkError("deployment did not return a stub id")

    def _invoke(
        self,
        function: Function[[int], int],
        scenario: LatencyScenario,
        cycle: int,
    ) -> LatencyPhaseSample:
        host_start = time.perf_counter()
        call = function.spawn(0)
        task_id = str(call.task_id)
        if not task_id:
            raise LatencyBenchmarkError(f"{scenario.value} invocation returned no task id")
        record = self._await_terminal_task(task_id)
        round_trip_ms = (time.perf_counter() - host_start) * 1000
        container = self._await_terminal_container(record.container_id or "")
        sample = LatencyPhaseSample(
            scenario=scenario,
            cycle=cycle,
            task_id=task_id,
            container_id=record.container_id or "",
            status=record.status,
            exit_code=record.exit_code,
            ok=record.status is TaskStatus.Complete and record.exit_code in (None, 0),
            created_at=record.created_at,
            container_started_at=container.started_at if container is not None else None,
            started_at=record.started_at,
            finished_at=record.finished_at,
        )
        if not sample.ok:
            sample.error = record.error or f"task ended {record.status.value}"
        self._fill_task_legs(sample, record)
        self._fill_container_legs(sample, record, container)
        sample.phase_ms[HOST_ROUND_TRIP] = round_trip_ms
        self._fill_event_phases(sample)
        return sample

    def _await_terminal_task(self, task_id: str) -> TaskResponse:
        deadline = time.monotonic() + self.config.invoke_timeout_seconds
        last: TaskResponse | None = None
        while time.monotonic() < deadline:
            last = TaskResponse.model_validate(
                self._request_json("GET", f"/api/v1/tasks/{urllib.parse.quote(task_id)}")
            )
            if last.status in _TERMINAL_STATUSES:
                return last
            time.sleep(self.config.poll_interval_seconds)
        status = last.status.value if last is not None else "unknown"
        raise LatencyBenchmarkError(
            f"task {task_id} did not reach a terminal status (last: {status})"
        )

    def _await_terminal_container(self, container_id: str) -> ContainerDetailResponse | None:
        if not container_id:
            return None
        deadline = time.monotonic() + self.config.invoke_timeout_seconds
        last: ContainerDetailResponse | None = None
        path = f"/api/v1/containers/{urllib.parse.quote(container_id)}"
        while time.monotonic() < deadline:
            last = ContainerDetailResponse.model_validate(self._request_json("GET", path))
            if last.status in {
                ContainerStatus.Exited,
                ContainerStatus.Failed,
                ContainerStatus.Stopped,
            }:
                return last
            time.sleep(self.config.poll_interval_seconds)
        status = last.status.value if last is not None else "unknown"
        raise LatencyBenchmarkError(
            f"container {container_id} did not reach a terminal status (last: {status})"
        )

    @staticmethod
    def _fill_task_legs(sample: LatencyPhaseSample, record: TaskResponse) -> None:
        created = record.created_at
        started = record.started_at
        finished = record.finished_at
        if created is not None and started is not None:
            sample.phase_ms[LEG_CREATED_TO_STARTED] = _delta_ms(created, started)
        if started is not None and finished is not None:
            sample.phase_ms[LEG_STARTED_TO_FINISHED] = _delta_ms(started, finished)
        if created is not None and finished is not None:
            sample.phase_ms[LEG_CREATED_TO_FINISHED] = _delta_ms(created, finished)

    @staticmethod
    def _fill_container_legs(
        sample: LatencyPhaseSample,
        task: TaskResponse,
        container: ContainerDetailResponse | None,
    ) -> None:
        if container is None or container.started_at is None:
            return
        if task.created_at is not None:
            sample.phase_ms[LEG_CREATED_TO_CONTAINER_STARTED] = _delta_ms(
                task.created_at,
                container.started_at,
            )
        if task.started_at is not None:
            sample.phase_ms[LEG_CONTAINER_STARTED_TO_TASK_STARTED] = _delta_ms(
                container.started_at,
                task.started_at,
            )

    def _fill_event_phases(self, sample: LatencyPhaseSample) -> None:
        if not sample.container_id:
            return
        path = f"/api/v1/events/containers/{urllib.parse.quote(sample.container_id)}/summary"
        payload = self._request_json("GET", path)
        summary = payload.get("summary")
        if not isinstance(summary, dict):
            return
        for metric_key, value in summary.items():
            if isinstance(value, int | float) and not isinstance(value, bool):
                sample.phase_ms[str(metric_key)] = float(value)

    # -- auth + workspace ----------------------------------------------------

    def _resolve_admin_token(self) -> None:
        if self.config.admin_token_value:
            return
        payload = self._compose_tools_json(
            "lazycloud-admin",
            "--json",
            "token",
            "create",
            f"latency-bench-{self.run_id}",
            "--kind",
            "admin",
            "--workspace",
            "default",
            "--scope",
            "*",
            "--expires-in",
            "3600",
        )
        token = payload.get("token")
        record = payload.get("record")
        if not isinstance(token, str) or not token:
            raise LatencyBenchmarkError("compose token create omitted the raw token")
        if not isinstance(record, dict):
            raise LatencyBenchmarkError("compose token create omitted the token record id")
        record_id = record.get("id")
        if not isinstance(record_id, str):
            raise LatencyBenchmarkError("compose token create omitted the token record id")
        self.minted_admin_token = token
        self.minted_admin_token_id = record_id

    def _create_workspace(self) -> None:
        workspace = WorkspaceResponse.model_validate(
            self._request_json(
                "POST",
                "/api/v1/workspaces",
                token=self._admin_token,
                payload=WorkspaceCreateRequest(name=f"latency-bench-{self.run_id}").model_dump(
                    mode="json"
                ),
                expected=201,
            )
        )
        token = TokenCreateResponse.model_validate(
            self._request_json(
                "POST",
                "/api/v1/tokens",
                token=self._admin_token,
                query={
                    "workspace": workspace.id,
                    "token_type": "workspace",
                    "name": f"latency-bench-{self.run_id}",
                },
                expected=201,
            )
        )
        self.workspace = workspace
        self.workspace_token = token.token
        self._configure_sdk_environment()

    def _configure_sdk_environment(self) -> None:
        os.environ[GATEWAY_HTTP_URL_ENV] = self.config.endpoint
        os.environ[GATEWAY_TOKEN_ENV] = self.workspace_token
        os.environ[WORKSPACE_ID_ENV] = self._workspace.id

    # -- cleanup -------------------------------------------------------------

    def _cleanup(self) -> None:
        if self.workspace is not None:
            workspace_path = urllib.parse.quote(self.workspace.id, safe="")
            try:
                self._request_json(
                    "DELETE",
                    f"/api/v1/workspaces/{workspace_path}",
                    token=self._admin_token,
                    expected=204,
                )
                self.cleanup_notes.append(
                    "workspace deleted (cascades apps, deployments, and tasks)"
                )
            except Exception as exc:
                self.cleanup_errors.append(f"delete workspace: {exc}")
            self._verify_workspace_absent(workspace_path)
        for name in (GATEWAY_TOKEN_ENV, WORKSPACE_ID_ENV, GATEWAY_HTTP_URL_ENV):
            os.environ.pop(name, None)
        if self.minted_admin_token_id:
            try:
                self._compose_tools_json(
                    "lazycloud-admin",
                    "--json",
                    "token",
                    "revoke",
                    self.minted_admin_token_id,
                )
                self.cleanup_notes.append("ephemeral compose admin token revoked")
            except Exception as exc:
                self.cleanup_errors.append(f"revoke admin token: {exc}")

    def _verify_workspace_absent(self, workspace_path: str) -> None:
        if not self.minted_admin_token and not self.config.admin_token_value:
            return
        try:
            self._request_json(
                "GET",
                f"/api/v1/workspaces/{workspace_path}",
                token=self._admin_token,
                expected=404,
            )
            self.cleanup_notes.append("verified workspace is absent (404 after delete)")
        except Exception as exc:
            self.cleanup_errors.append(f"verify workspace absent: {exc}")

    # -- transport -----------------------------------------------------------

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        query: dict[str, str] | None = None,
        payload: dict[str, JsonValue] | None = None,
        expected: int = 200,
    ) -> dict[str, JsonValue]:
        url = f"{self.config.endpoint}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        bearer = token if token is not None else self.workspace_token
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.request_timeout_seconds
            ) as response:
                body = response.read()
                if response.status != expected:
                    raise LatencyBenchmarkError(
                        f"{method} {path} returned {response.status}, expected {expected}"
                    )
        except urllib.error.HTTPError as exc:
            if exc.code == expected:
                return {}
            detail = exc.read().decode("utf-8", errors="replace")
            message = f"{method} {path} failed with {exc.code}: {detail}"
            raise LatencyBenchmarkError(message) from exc
        if not body:
            return {}
        return _parse_json_object(body, source=f"{method} {path}")

    def _compose_tools_json(self, *args: str) -> dict[str, JsonValue]:
        completed = subprocess.run(
            [*self.config.compose_cli, "--profile", "tools", "run", "--rm", "cli", *args],
            cwd=Path(__file__).resolve().parents[2],
            check=False,
            capture_output=True,
            text=True,
            timeout=self.config.invoke_timeout_seconds,
        )
        if completed.returncode != 0:
            raise LatencyBenchmarkError(completed.stderr or "compose tools command failed")
        return _parse_json_object(completed.stdout, source="compose tools command")

    @property
    def _admin_token(self) -> str:
        token = self.config.admin_token_value or self.minted_admin_token
        if not token:
            raise LatencyBenchmarkError("an admin token is required")
        return token

    @property
    def _workspace(self) -> WorkspaceResponse:
        if self.workspace is None:
            raise LatencyBenchmarkError("workspace has not been created")
        return self.workspace


_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.Complete,
        TaskStatus.Failed,
        TaskStatus.Expired,
        TaskStatus.Cancelled,
        TaskStatus.Timeout,
    }
)


def _parse_json_object(payload: str | bytes, *, source: str) -> dict[str, JsonValue]:
    try:
        return _JSON_OBJECT_ADAPTER.validate_json(payload)
    except ValidationError as exc:
        raise LatencyBenchmarkError(f"{source} did not return a JSON object") from exc


def _delta_ms(start: datetime, end: datetime) -> float:
    return max((end - start).total_seconds() * 1000, 0.0)


def _summarize_scenarios(
    samples: list[LatencyPhaseSample],
) -> list[LatencyScenarioReport]:
    reports: list[LatencyScenarioReport] = []
    for scenario in LatencyScenario:
        scoped = [sample for sample in samples if sample.scenario == scenario]
        ok = [sample for sample in scoped if sample.ok]
        present: set[str] = {key for sample in ok for key in sample.phase_ms}
        phases: dict[str, LatencySummary] = {}
        for phase in present:
            values = [sample.phase_ms[phase] for sample in ok if phase in sample.phase_ms]
            summary = summarize_values(values)
            if summary is not None:
                phases[phase] = summary
        reports.append(
            LatencyScenarioReport(
                scenario=scenario,
                samples=len(scoped),
                ok_samples=len(ok),
                phase_order=_ordered_phase_keys(set(phases)),
                phases=phases,
            )
        )
    return reports


def run_latency_benchmark(config: LatencyConfig) -> LatencyReport:
    report = LatencyBenchmark(config).run()
    if config.output is not None:
        config.output.parent.mkdir(parents=True, exist_ok=True)
        config.output.write_text(report.to_json(), encoding="utf-8")
    return report


__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_RUNS",
    "LatencyBenchmark",
    "LatencyBenchmarkError",
    "LatencyConfig",
    "LatencyPhaseSample",
    "LatencyReport",
    "LatencyScenario",
    "LatencyScenarioReport",
    "run_latency_benchmark",
]
