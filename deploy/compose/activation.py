"""Activate the connected-AWS Compose stack in one command.

Raw ``docker compose up`` leaves the control plane and scheduler without AWS
credentials, so connection validation can never succeed (deploy/AGENTS.md,
"Connected-AWS acceptance environment"). This entrypoint is the canonical
activation path: it validates the scoped AWS configuration directory, asserts
the stack-owned control role through STS, verifies the control CloudFormation
stack, then applies ``deploy/compose/aws-profile.yaml`` to recreate only the
credential-consuming services and waits for their health.

Conformance with the accepted repair contract (board/loop.md, 2026-07-23):

- The stack-owned non-root control role is validated: STS must report an
  assumed-role identity for the exact role configured on the selected profile,
  never root or bare long-lived user credentials.
- Only the credential-consuming services (default ``control-plane`` and
  ``scheduler``) are recreated, with ``--no-build --no-deps``.
- The entrypoint is directly runnable (``uv run python
  deploy/compose/activation.py``) and importable (``deploy.compose.activation``)
  without a stdlib-shadowing filename or dynamic import.
- Credentials are the SDK role chain and auto-refresh inside the services; no
  fixed session expiry is created or recorded. The command never creates,
  copies, or reads credential file contents — it only consumes an existing
  scoped configuration directory (deploy/AGENTS.md describes how to create
  one).

Exit codes: 0 success, 1 failure, 77 when the AWS CLI or the scoped
configuration directory prerequisite is absent.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, Field, RootModel, ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILES = ("compose.yaml", "deploy/compose/aws-profile.yaml")
_HEALTHY_STACK_STATUSES = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE"})
_STATIC_CREDENTIAL_VARIABLES = frozenset(
    {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
)
_TERMINAL_SERVICE_STATES = frozenset({"dead", "exited", "removing"})
_PREREQUISITE_EXIT = 77
_HEALTH_DEADLINE_SECONDS = 180.0
_HEALTH_POLL_SECONDS = 5.0


class ActivationError(RuntimeError):
    pass


class PrerequisiteError(ActivationError):
    pass


class AwsCallerIdentity(BaseModel):
    arn: str = Field(alias="Arn")


class StackStatusDocument(RootModel[str]):
    pass


class ComposeServiceStatus(BaseModel):
    service: str = Field(alias="Service")
    state: str = Field(alias="State")
    health: str = Field(default="", alias="Health")


class ComposeStatusList(RootModel[list[ComposeServiceStatus]]):
    pass


def _error_detail(stderr: str) -> str:
    stripped = stderr.strip()
    return stripped.splitlines()[-1][:500] if stripped else "no error detail"


def _run(
    command: Sequence[str],
    environment: Mapping[str, str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        env=dict(environment),
        cwd=cwd,
    )


def _dotenv_values(path: Path) -> dict[str, str]:
    """Read non-secret defaults from the repository ``.env`` without printing values."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw_value = stripped.partition("=")
        values[key.strip()] = raw_value.strip().strip("'\"")
    return values


def _require_config_dir(raw_path: str) -> Path:
    config_dir = Path(raw_path).expanduser()
    if not (config_dir / "config").is_file():
        raise PrerequisiteError(
            f"scoped AWS configuration directory {config_dir} has no config file; create a "
            "directory containing only the test source credentials and the role-chain profiles "
            "as described in deploy/AGENTS.md (Connected-AWS acceptance environment) — this "
            "command never creates or copies credential files"
        )
    for name in ("config", "credentials"):
        candidate = config_dir / name
        if candidate.is_file():
            mode = candidate.stat().st_mode & 0o777
            if mode & 0o077:
                print(
                    f"warning: {candidate} mode {mode:03o} allows group/other access; expected 600",
                    file=sys.stderr,
                )
    return config_dir


def _expected_role_name(config_path: Path, profile: str) -> str:
    """Derive the control role name from the profile's ``role_arn`` in the config file.

    Only the configuration file is parsed; the credentials file is never read.
    """
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    section = f"profile {profile}"
    if not parser.has_section(section):
        raise ActivationError(
            f"profile {profile!r} is not defined in {config_path}; see deploy/AGENTS.md "
            "(Connected-AWS acceptance environment) for the scoped role-chain layout"
        )
    role_arn = parser.get(section, "role_arn", fallback="")
    if "/" not in role_arn:
        raise ActivationError(
            f"profile {profile!r} in {config_path} has no role_arn; the activation profile "
            "must assume the stack-owned control role"
        )
    return role_arn.rsplit("/", 1)[-1]


def _aws_environment(config_dir: Path, profile: str) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if key not in _STATIC_CREDENTIAL_VARIABLES
    }
    environment.update(
        AWS_PAGER="",
        AWS_PROFILE=profile,
        AWS_CONFIG_FILE=str(config_dir / "config"),
        AWS_SHARED_CREDENTIALS_FILE=str(config_dir / "credentials"),
        AWS_SDK_LOAD_CONFIG="1",
    )
    return environment


def _validated_role_arn(
    aws_cli: str,
    environment: Mapping[str, str],
    expected_role: str,
) -> str:
    result = _run([aws_cli, "sts", "get-caller-identity", "--output", "json"], environment)
    if result.returncode != 0:
        raise ActivationError(
            f"sts get-caller-identity failed for the scoped profile: {_error_detail(result.stderr)}"
        )
    arn = AwsCallerIdentity.model_validate(json.loads(result.stdout)).arn
    if f":assumed-role/{expected_role}/" not in arn:
        raise ActivationError(
            f"caller identity is not an assumed-role session of the control role "
            f"{expected_role!r}; refusing to activate with a root or unexpected identity"
        )
    return arn


def _control_stack_status(
    aws_cli: str,
    environment: Mapping[str, str],
    region: str,
    stack_name: str,
) -> str:
    result = _run(
        [
            aws_cli,
            "cloudformation",
            "describe-stacks",
            "--region",
            region,
            "--stack-name",
            stack_name,
            "--query",
            "Stacks[0].StackStatus",
            "--output",
            "json",
        ],
        environment,
    )
    if result.returncode != 0:
        stderr = result.stderr
        if "AccessDenied" in stderr or "not authorized" in stderr:
            print(
                f"warning: the operator role lacks describe-stacks on control stack "
                f"{stack_name}; proceeding without stack verification",
                file=sys.stderr,
            )
            return "unverified (describe-stacks denied to the operator role)"
        raise ActivationError(
            f"cloudformation describe-stacks failed for {stack_name}: {_error_detail(stderr)}"
        )
    status = StackStatusDocument.model_validate(json.loads(result.stdout)).root
    if status not in _HEALTHY_STACK_STATUSES:
        raise ActivationError(
            f"control stack {stack_name} in {region} is {status}; expected "
            "CREATE_COMPLETE or UPDATE_COMPLETE"
        )
    return status


def _compose_environment(config_dir: Path, profile: str) -> dict[str, str]:
    return {
        **os.environ,
        "AWS_PROFILE": profile,
        "LAZYCLOUD_COMPOSE_AWS_CONFIG_DIR": str(config_dir),
    }


# Services whose `network_mode: service:<key>` binds them to another container's
# network namespace; recreating the key requires recreating these too.
_NAMESPACE_DEPENDENTS: Mapping[str, tuple[str, ...]] = {
    "control-plane": ("tailnet-gateway",),
}


def _compose_command(*arguments: str) -> list[str]:
    command = ["docker", "compose"]
    for compose_file in _COMPOSE_FILES:
        command.extend(["-f", compose_file])
    command.extend(arguments)
    return command


def _apply_compose(services: Sequence[str], environment: Mapping[str, str]) -> None:
    result = _run(
        _compose_command("up", "-d", "--no-build", "--no-deps", *services),
        environment,
        cwd=_REPO_ROOT,
    )
    if result.returncode != 0:
        raise ActivationError(f"docker compose up failed: {_error_detail(result.stderr)}")
    _recreate_namespace_dependents(services, environment)


def _recreate_namespace_dependents(
    services: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    """Recreate services that share a recreated service's network namespace.

    A ``network_mode: service:<name>`` container keeps the namespace of the exact
    container it started with. Recreating that target leaves the dependent
    attached to a destroyed namespace, so it stays "healthy" while losing all
    connectivity — this silently took the public Tailnet gateway offline during
    live acceptance. ``--no-deps`` never recreates them, so do it explicitly.
    """
    dependents = sorted(
        {dependent for service in services for dependent in _NAMESPACE_DEPENDENTS.get(service, ())}
    )
    if not dependents:
        return
    result = _run(
        _compose_command("up", "-d", "--no-build", "--no-deps", "--force-recreate", *dependents),
        environment,
        cwd=_REPO_ROOT,
    )
    if result.returncode != 0:
        raise ActivationError(
            "docker compose up failed for namespace dependents "
            f"{', '.join(dependents)}: {_error_detail(result.stderr)}"
        )


def _parse_compose_statuses(stdout: str) -> list[ComposeServiceStatus]:
    text = stdout.strip()
    if not text:
        return []
    try:
        document: object = json.loads(text)
    except json.JSONDecodeError:
        document = [json.loads(line) for line in text.splitlines() if line.strip()]
    try:
        return ComposeStatusList.model_validate(document).root
    except ValidationError:
        return [ComposeServiceStatus.model_validate(document)]


def _await_service_health(
    services: Sequence[str],
    environment: Mapping[str, str],
) -> dict[str, str]:
    deadline = time.monotonic() + _HEALTH_DEADLINE_SECONDS
    while True:
        result = _run(
            _compose_command("ps", "--all", "--format", "json", *services),
            environment,
            cwd=_REPO_ROOT,
        )
        if result.returncode != 0:
            raise ActivationError(f"docker compose ps failed: {_error_detail(result.stderr)}")
        statuses = {entry.service: entry for entry in _parse_compose_statuses(result.stdout)}
        health: dict[str, str] = {}
        pending: list[str] = []
        for service in services:
            entry = statuses.get(service)
            if entry is None:
                health[service] = "absent"
                pending.append(service)
                continue
            if entry.state in _TERMINAL_SERVICE_STATES:
                raise ActivationError(
                    f"service {service} entered terminal state {entry.state!r} while waiting "
                    "for health"
                )
            health[service] = entry.health or entry.state
            if entry.state != "running" or entry.health not in ("", "healthy"):
                pending.append(service)
        if not pending:
            return health
        if time.monotonic() >= deadline:
            raise ActivationError(
                "timed out waiting for healthy services: " + ", ".join(sorted(pending))
            )
        time.sleep(_HEALTH_POLL_SECONDS)


def _activate(args: argparse.Namespace, dotenv: Mapping[str, str]) -> dict[str, object]:
    if shutil.which(args.aws_cli) is None:
        raise PrerequisiteError(f"the AWS CLI {args.aws_cli!r} is not installed")
    config_dir = _require_config_dir(args.aws_config_dir)
    expected_role = _expected_role_name(config_dir / "config", args.profile)
    aws_environment = _aws_environment(config_dir, args.profile)
    assumed_role_arn = _validated_role_arn(args.aws_cli, aws_environment, expected_role)
    stack_status = _control_stack_status(
        args.aws_cli, aws_environment, args.region, args.control_stack_name
    )
    services: list[str] = list(args.services)
    if args.skip_compose:
        service_health = {service: "skipped (--skip-compose)" for service in services}
    else:
        if shutil.which("docker") is None:
            raise ActivationError("docker is not installed; cannot apply the Compose overlay")
        compose_environment = _compose_environment(config_dir, args.profile)
        _apply_compose(services, compose_environment)
        service_health = _await_service_health(services, compose_environment)
    return {
        "assumed_role_arn": assumed_role_arn,
        "aws_config_dir": str(config_dir),
        "compose_applied": not args.skip_compose,
        "control_stack": {"name": args.control_stack_name, "status": stack_status},
        "credential_mode": "sdk-role-chain (auto-refreshing)",
        "profile": args.profile,
        "region": args.region,
        "services": service_health,
    }


def main(argv: Sequence[str] | None = None) -> int:
    dotenv = _dotenv_values(_REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--aws-config-dir",
        default=os.environ.get("LAZYCLOUD_COMPOSE_AWS_CONFIG_DIR", "~/.lazycloud/compose-aws"),
        help="scoped AWS configuration directory mounted into the services",
    )
    parser.add_argument("--profile", default="compose-control")
    parser.add_argument(
        "--control-stack-name",
        default=os.environ.get("LAZYCLOUD_AWS_CONTROL_STACK_NAME")
        or dotenv.get("LAZYCLOUD_AWS_CONTROL_STACK_NAME"),
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or dotenv.get("AWS_REGION") or "us-east-1",
    )
    parser.add_argument("--aws-cli", default="aws")
    parser.add_argument(
        "--services",
        nargs="+",
        default=["control-plane", "scheduler"],
        help="credential-consuming services to recreate and health-check",
    )
    parser.add_argument(
        "--skip-compose",
        action="store_true",
        help="validate the credentials and control stack without touching Compose",
    )
    args = parser.parse_args(argv)
    if not args.control_stack_name:
        parser.error("--control-stack-name is required (or set LAZYCLOUD_AWS_CONTROL_STACK_NAME)")
    try:
        report = _activate(args, dotenv)
    except PrerequisiteError as exc:
        print(f"activation prerequisite missing: {exc}", file=sys.stderr)
        return _PREREQUISITE_EXIT
    except (ActivationError, ValueError) as exc:
        print(f"activation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
