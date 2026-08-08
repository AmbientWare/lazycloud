from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path

from benchmarks.harness.latency import (
    DEFAULT_ENDPOINT,
    DEFAULT_RUNS,
    LatencyConfig,
    run_latency_benchmark,
)
from benchmarks.harness.models import BenchmarkKind
from benchmarks.harness.reports import report_to_json, report_to_markdown
from benchmarks.harness.runner import run_named_suite, run_suite
from benchmarks.harness.sandbox_parallel import (
    DEFAULT_CONTAINER_COMMAND,
    DEFAULT_EXEC_COMMAND,
    DEFAULT_EXPECTED_OUTPUT_TEMPLATE,
    DEFAULT_SANDBOX_IMAGE,
    DEFAULT_SANDBOX_NAME,
    SandboxParallelConfig,
    build_sandbox_parallel_plan,
    config_from_env,
    run_sandbox_parallel,
    write_sandbox_parallel_outputs,
)
from benchmarks.harness.suites import DEFAULT_CASES, available_suites


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmark-harness")
    case_choices = [item.kind.value for item in DEFAULT_CASES]
    parser.add_argument(
        "--case",
        dest="cases",
        action="append",
        choices=case_choices,
        help="Benchmark case to run. May be passed multiple times.",
    )
    parser.add_argument("--suite", help="Named suite definition to run.")
    parser.add_argument(
        "--list-suites",
        action="store_true",
        help="List named suite definitions.",
    )
    parser.add_argument(
        "--sandbox-parallel",
        action="store_true",
        help="Plan or run the parallel sandbox operational benchmark.",
    )
    parser.add_argument(
        "--latency",
        action="store_true",
        help="Run the container dispatch-latency benchmark against the Compose stack.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        help="Latency benchmark deploy-cycle count (per-scenario samples).",
    )
    parser.add_argument(
        "--admin-token",
        help="Administrator credential for the latency benchmark, required because the "
        "workspace it creates is owned by the account that creates it. Prefer "
        "BENCHMARK_ADMIN_TOKEN.",
    )
    parser.add_argument(
        "--invoke-timeout-seconds",
        type=float,
        help="Per-invocation terminal-task timeout seconds.",
    )
    parser.add_argument(
        "--idle-seconds",
        type=float,
        help="Latency benchmark delay before the idle-fresh one-shot invocation.",
    )
    parser.add_argument(
        "--run-live",
        action="store_true",
        help="Create real sandboxes/functions against the configured control plane.",
    )
    parser.add_argument("--count", type=int, help="Sandbox measured sample count.")
    parser.add_argument("--parallelism", type=int, help="Sandbox concurrent create count.")
    parser.add_argument("--warmup", type=int, help="Sandbox warmup sample count.")
    parser.add_argument("--prewarm-count", type=int, help="Sandbox pool prewarm sample count.")
    parser.add_argument(
        "--prewarm-parallelism",
        type=int,
        help="Sandbox pool prewarm concurrency. Defaults to measured parallelism.",
    )
    parser.add_argument("--endpoint", help="Control-plane endpoint.")
    parser.add_argument("--workspace-name", dest="workspace_name", help="Workspace name.")
    parser.add_argument("--token", help="Bearer token. Prefer config or environment.")
    parser.add_argument("--sandbox-name", help="Sandbox deployment name.")
    parser.add_argument("--image", help="Sandbox base image URI.")
    parser.add_argument("--image-id", help="Prepared image id.")
    parser.add_argument(
        "--container-command",
        help="Sandbox keepalive command, parsed with shell-style quoting.",
    )
    parser.add_argument(
        "--exec",
        dest="exec_command",
        help="Readiness command to run in each sandbox.",
    )
    parser.add_argument("--exec-cwd", help="Readiness command working directory.")
    parser.add_argument("--expected-output", help="Readiness stdout template.")
    parser.add_argument("--output-tail-bytes", type=int, help="Captured stdout/stderr tail bytes.")
    parser.add_argument("--cpu", type=float, help="Sandbox CPU request.")
    parser.add_argument("--memory", help="Sandbox memory request.")
    parser.add_argument("--gpu", help="Sandbox GPU type.")
    parser.add_argument("--gpu-count", type=int, help="Sandbox GPU count.")
    parser.add_argument("--keep-warm-seconds", type=int, help="Sandbox keep-warm seconds.")
    parser.add_argument(
        "--cleanup-ttl-seconds",
        type=int,
        help="TTL set before terminating samples.",
    )
    parser.add_argument("--timeout-seconds", type=float, help="SDK request timeout seconds.")
    parser.add_argument("--ready-timeout-seconds", type=float, help="Readiness timeout seconds.")
    parser.add_argument("--create-retries", type=int, help="Sandbox create retry count.")
    parser.add_argument(
        "--retry-interval-seconds",
        type=float,
        help="Create/readiness retry interval.",
    )
    parser.add_argument("--prepare-sandbox", dest="prepare_sandbox", action="store_true")
    parser.add_argument("--no-prepare-sandbox", dest="prepare_sandbox", action="store_false")
    parser.set_defaults(prepare_sandbox=None)
    parser.add_argument("--wait-running", dest="wait_running", action="store_true")
    parser.add_argument("--no-wait-running", dest="wait_running", action="store_false")
    parser.set_defaults(wait_running=None)
    parser.add_argument("--wait-exec-complete", dest="wait_exec_complete", action="store_true")
    parser.add_argument("--no-wait-exec-complete", dest="wait_exec_complete", action="store_false")
    parser.set_defaults(wait_exec_complete=None)
    parser.add_argument("--keep-sandboxes", action="store_true", help="Do not terminate samples.")
    parser.add_argument("--output", type=Path, help="Write sandbox benchmark JSON.")
    parser.add_argument("--report", type=Path, help="Write sandbox benchmark markdown.")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--json", action="store_true", help="Print JSON instead of markdown.")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.list_suites:
        print("\n".join(available_suites()))
        return
    if args.suite and args.cases:
        raise SystemExit("--suite cannot be combined with --case")
    if args.sandbox_parallel and (args.suite or args.cases):
        raise SystemExit("--sandbox-parallel cannot be combined with --suite or --case")
    if args.latency and (args.suite or args.cases or args.sandbox_parallel):
        raise SystemExit("--latency cannot be combined with other benchmark modes")
    if args.latency:
        report = run_latency_benchmark(_latency_config(args))
        output = report.to_json() if args.json else report.to_markdown()
        print(output, end="")
        return
    if args.sandbox_parallel:
        config = _sandbox_parallel_config(args)
        if config.run_live:
            result = run_sandbox_parallel(config, workspace=args.workspace)
            output = result.to_json() if args.json else result.to_markdown()
        else:
            plan = build_sandbox_parallel_plan(config)
            write_sandbox_parallel_outputs(plan)
            output = plan.to_json() if args.json else plan.to_markdown()
        print(output, end="")
        return
    cases = [BenchmarkKind(item) for item in args.cases] if args.cases else None
    report = (
        run_named_suite(args.suite, workspace=args.workspace)
        if args.suite
        else run_suite(cases, workspace=args.workspace)
    )
    output = report_to_json(report) if args.json else report_to_markdown(report)
    print(output, end="")


def _latency_config(args: argparse.Namespace) -> LatencyConfig:
    admin_token = args.admin_token or os.getenv("BENCHMARK_ADMIN_TOKEN")
    defaults = LatencyConfig()
    return LatencyConfig(
        run_live=bool(args.run_live),
        endpoint=(args.endpoint or os.getenv("BENCHMARK_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/"),
        runs=args.runs if args.runs is not None else DEFAULT_RUNS,
        admin_token=_secret_token(admin_token),
        idle_seconds=(
            args.idle_seconds if args.idle_seconds is not None else defaults.idle_seconds
        ),
        invoke_timeout_seconds=(
            args.invoke_timeout_seconds
            if args.invoke_timeout_seconds is not None
            else defaults.invoke_timeout_seconds
        ),
        request_timeout_seconds=(
            args.timeout_seconds
            if args.timeout_seconds is not None
            else defaults.request_timeout_seconds
        ),
        output=args.output,
    )


def _sandbox_parallel_config(args: argparse.Namespace) -> SandboxParallelConfig:
    base = config_from_env()
    command = _split_optional_command(args.exec_command, default=DEFAULT_EXEC_COMMAND)
    container_command = _split_optional_command(
        args.container_command,
        default=DEFAULT_CONTAINER_COMMAND,
    )
    return SandboxParallelConfig(
        count=args.count if args.count is not None else base.count,
        parallelism=args.parallelism if args.parallelism is not None else base.parallelism,
        warmup=args.warmup if args.warmup is not None else base.warmup,
        prewarm_count=(
            args.prewarm_count if args.prewarm_count is not None else base.prewarm_count
        ),
        prewarm_parallelism=(
            args.prewarm_parallelism
            if args.prewarm_parallelism is not None
            else base.prewarm_parallelism
        ),
        endpoint=args.endpoint or base.endpoint,
        workspace=args.workspace_name or base.workspace,
        token=_secret_token(args.token) or base.token,
        sandbox_name=args.sandbox_name or base.sandbox_name or DEFAULT_SANDBOX_NAME,
        image=args.image or base.image or DEFAULT_SANDBOX_IMAGE,
        image_id=args.image_id or base.image_id,
        container_command=container_command if args.container_command else base.container_command,
        exec_command=command if args.exec_command else base.exec_command,
        exec_cwd=args.exec_cwd or base.exec_cwd,
        expected_output_template=(
            args.expected_output
            or base.expected_output_template
            or DEFAULT_EXPECTED_OUTPUT_TEMPLATE
        ),
        output_tail_bytes=(
            args.output_tail_bytes if args.output_tail_bytes is not None else base.output_tail_bytes
        ),
        cpu=args.cpu if args.cpu is not None else base.cpu,
        memory=args.memory if args.memory is not None else base.memory,
        gpu=args.gpu if args.gpu is not None else base.gpu,
        gpu_count=args.gpu_count if args.gpu_count is not None else base.gpu_count,
        keep_warm_seconds=(
            args.keep_warm_seconds if args.keep_warm_seconds is not None else base.keep_warm_seconds
        ),
        cleanup_ttl_seconds=(
            args.cleanup_ttl_seconds
            if args.cleanup_ttl_seconds is not None
            else base.cleanup_ttl_seconds
        ),
        request_timeout_seconds=(
            args.timeout_seconds
            if args.timeout_seconds is not None
            else base.request_timeout_seconds
        ),
        ready_timeout_seconds=(
            args.ready_timeout_seconds
            if args.ready_timeout_seconds is not None
            else base.ready_timeout_seconds
        ),
        create_retries=(
            args.create_retries if args.create_retries is not None else base.create_retries
        ),
        retry_interval_seconds=(
            args.retry_interval_seconds
            if args.retry_interval_seconds is not None
            else base.retry_interval_seconds
        ),
        prepare_sandbox=(
            args.prepare_sandbox if args.prepare_sandbox is not None else base.prepare_sandbox
        ),
        wait_running=args.wait_running if args.wait_running is not None else base.wait_running,
        wait_exec_complete=(
            args.wait_exec_complete
            if args.wait_exec_complete is not None
            else base.wait_exec_complete
        ),
        keep_sandboxes=args.keep_sandboxes or base.keep_sandboxes,
        run_live=args.run_live or base.run_live,
        output=args.output or base.output,
        report=args.report or base.report,
    )


def _split_optional_command(value: str | None, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    try:
        return tuple(shlex.split(value))
    except ValueError as exc:
        raise SystemExit(f"invalid command {value!r}: {exc}") from exc


def _secret_token(value: str | None):
    if not value:
        return None
    from pydantic import SecretStr

    return SecretStr(value)
