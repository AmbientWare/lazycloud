"""Run Claude Code headless inside a sandbox, as an unprivileged user, and read its result."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from lazycloud import Sandbox, SandboxInstance
from pydantic import BaseModel, ConfigDict, ValidationError

from coding_agents.settings import (
    AGENT_MAX_BUDGET_USD,
    AGENT_MAX_TURNS,
    AGENT_MODEL,
    AGENT_TIMEOUT_SECONDS,
)

AGENT_USER = "agent"
AGENT_HOME = "/home/agent"
AGENT_ENV = {"HOME": AGENT_HOME}
REPO_DIR = f"{AGENT_HOME}/repo"

# Anthropic's published inbound range for its API. Agent sandboxes reach
# nothing else, DNS included, so the API hostname is pinned in /etc/hosts.
MODEL_API_HOST = "api.anthropic.com"
MODEL_API_RANGE = "160.79.104.0/23"


class SandboxCommandError(RuntimeError):
    pass


class ModelApiAddressError(RuntimeError):
    pass


class AgentOutputError(RuntimeError):
    pass


class AgentRun(BaseModel):
    """The fields of Claude Code's JSON result this system reads."""

    model_config = ConfigDict(extra="ignore")

    subtype: str
    is_error: bool
    result: str = ""
    num_turns: int
    total_cost_usd: float


@contextmanager
def started(sandbox: Sandbox) -> Iterator[SandboxInstance]:
    """A running sandbox, terminated however the block ends so it stops billing."""
    instance = sandbox.create(timeout_seconds=600)
    try:
        yield instance
    finally:
        instance.terminate()


def as_agent(*args: str) -> list[str]:
    """The command run as the agent user, which cannot change /etc/hosts or root's files."""
    return ["setpriv", f"--reuid={AGENT_USER}", f"--regid={AGENT_USER}", "--init-groups", *args]


def run_checked(
    instance: SandboxInstance,
    command: list[str],
    *,
    cwd: str,
    timeout_seconds: float,
    env: dict[str, str] | None = None,
) -> str:
    """Run a command to completion and return its stdout; a non-zero exit raises."""
    response = instance.run(command, cwd=cwd, env=env, timeout_seconds=timeout_seconds)
    if response.exit_code != 0:
        output = (response.stdout + response.stderr)[-2000:]
        raise SandboxCommandError(f"{command[0]} exited with {response.exit_code}: {output}")
    return response.stdout


def address_in_range(addresses: Iterable[str], network: str) -> str:
    """The first address inside network, so the allow list covers the pinned host."""
    allowed = ipaddress.ip_network(network)
    for address in addresses:
        if ipaddress.ip_address(address) in allowed:
            return address
    raise ModelApiAddressError(
        f"{MODEL_API_HOST} resolves outside {network}; update MODEL_API_RANGE"
    )


def pin_model_api(instance: SandboxInstance) -> None:
    """Resolve the model API here, where DNS works, and pin the address in the sandbox."""
    resolved = socket.getaddrinfo(MODEL_API_HOST, 443, socket.AF_INET, socket.SOCK_STREAM)
    address = address_in_range((str(entry[4][0]) for entry in resolved), MODEL_API_RANGE)
    run_checked(
        instance,
        ["sh", "-c", 'printf "%s %s\\n" "$1" "$2" >> /etc/hosts', "sh", address, MODEL_API_HOST],
        cwd="/",
        timeout_seconds=30,
    )


def run_agent(instance: SandboxInstance, *, cwd: str, prompt: str, api_key: str) -> AgentRun:
    """Run one headless Claude Code session in cwd and return its result.

    --bare skips hooks, plugins and MCP servers found on disk, so a run depends
    only on these flags. The turn, budget and time limits each end a run early.
    """
    command = as_agent(
        "claude",
        "--bare",
        "--print",
        "--output-format",
        "json",
        "--model",
        AGENT_MODEL,
        "--max-turns",
        str(AGENT_MAX_TURNS),
        "--max-budget-usd",
        f"{AGENT_MAX_BUDGET_USD:.2f}",
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
        prompt,
    )
    env = {
        **AGENT_ENV,
        "ANTHROPIC_API_KEY": api_key,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    response = instance.run(command, cwd=cwd, env=env, timeout_seconds=AGENT_TIMEOUT_SECONDS)
    return parse_agent_output(response.stdout, response.stderr, response.exit_code)


def parse_agent_output(stdout: str, stderr: str, exit_code: int) -> AgentRun:
    """Claude Code prints one JSON result, also when it stops at a limit.

    A limit sets subtype; an API failure, such as a rejected key, keeps subtype
    "success" and sets is_error, and raises here.
    """
    try:
        run = AgentRun.model_validate_json(stdout.strip())
    except ValidationError as exc:
        raise AgentOutputError(
            f"claude exited with {exit_code} without a JSON result: {(stdout + stderr)[-2000:]}"
        ) from exc
    if run.is_error and run.subtype == "success":
        raise AgentOutputError(f"claude failed: {run.result[-2000:]}")
    return run
