"""Coding agents: a devbox to work with them, GitHub issues turned into pull
requests in sandboxes, and a nightly eval of the agent.

Store the secrets with `uv run python -m coding_agents.configure`, deploy with
`uv run lazycloud deploy coding_agents.app:app`, then build the repository
snapshot with `uv run python -m coding_agents.snapshot`.
"""

import os
from datetime import UTC, datetime

from lazycloud import AgentHarness, App, Autoscaler, Image, Map, Sandbox, Secret
from lazycloud.exceptions import SdkError

from coding_agents.agent import MODEL_API_RANGE, started
from coding_agents.evals import EvalReport, EvalResult, run_eval_task, task_names
from coding_agents.github import GitHub
from coding_agents.github_events import IssueJob
from coding_agents.issues import IssueOutcome, RepoSnapshot, solve_issue
from coding_agents.settings import (
    AGENT_TIMEOUT_SECONDS,
    EVAL_ISSUE,
    MAX_PARALLEL_ISSUES,
    MAX_WAITING_ISSUES,
    REPOSITORY,
)
from coding_agents.webhook import create_api

APP_NAME = "coding_agents"
app = App(APP_NAME)

ANTHROPIC_API_KEY = Secret("ANTHROPIC_API_KEY")
GITHUB_TOKEN = Secret("GITHUB_TOKEN")
GITHUB_WEBHOOK_SECRET = Secret("GITHUB_WEBHOOK_SECRET")

# The repository snapshot record, and one claim per issue an agent is working on.
STATE = Map("coding-agents")
SNAPSHOT_KEY = "repo-snapshot"
CLAIM_TTL_SECONDS = 2 * 60 * 60

# Functions run this project's locked dependencies, plus git to push branches.
function_image = Image.from_uv(".").add_commands(
    [
        "apt-get update && apt-get install -y --no-install-recommends git "
        "&& rm -rf /var/lib/apt/lists/*"
    ]
)

# Agents work in this image as the unprivileged `agent` user, with Claude Code
# on a checksum-verified Node.js and uv and pytest for Python repositories.
agent_image = (
    Image(python_version="3.12")
    .add_commands(
        [
            "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "--no-install-recommends ca-certificates curl git xz-utils "
            "&& rm -rf /var/lib/apt/lists/*",
            "curl -fsSL -o /tmp/node.tar.xz "
            "https://nodejs.org/dist/v22.23.3/node-v22.23.3-linux-x64.tar.xz "
            "&& echo 'df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de  "
            "/tmp/node.tar.xz' | sha256sum --check "
            "&& tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 "
            "&& rm /tmp/node.tar.xz",
            "npm install --global --no-audit --no-fund @anthropic-ai/claude-code@2.1.282 "
            "&& npm cache clean --force",
            "useradd --create-home --shell /bin/bash agent",
        ]
    )
    .add_python_packages(["uv==0.11.32", "pytest==9.1.1"])
)

# You work with an agent here over SSH. The root disk keeps clones, logins and
# shell history between sessions; an hour of idle time lets an agent left
# running in tmux finish after you disconnect.
devbox = app.devbox(
    "agent-box",
    image=Image(python_version="3.12")
    .add_commands(
        [
            "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "--no-install-recommends gh jq ripgrep tmux && rm -rf /var/lib/apt/lists/*"
        ]
    )
    .add_python_packages(["uv==0.11.32"]),
    agent_harnesses=[AgentHarness.ClaudeCode, AgentHarness.Codex],
    disk="50Gi",
    cpu=2,
    memory="8Gi",
    keep_warm=60 * 60,
    secrets=[ANTHROPIC_API_KEY.name, GITHUB_TOKEN.name],
)

# Clones the repository and installs its dependencies, so it is the only agent
# sandbox with open network. Its filesystem becomes the repository snapshot.
repo_builder = app.sandbox(
    name="repo-builder",
    image=agent_image,
    cpu=2,
    memory="4Gi",
    keep_warm_seconds=30 * 60,
)

# Each eval task starts from the clean agent image. It reaches only the model API.
eval_sandbox = app.sandbox(
    name="eval",
    image=agent_image,
    cpu=0.5,
    memory="1Gi",
    allow_list=[MODEL_API_RANGE],
    keep_warm_seconds=300,
    preemptible=False,
)


def issue_sandbox(image_id: str) -> Sandbox:
    """A sandbox on the repository snapshot that reaches only the model API.

    The image is the newest snapshot, known only at run time, so each run
    declares the sandbox on its own App object under this app's name.
    """
    return App(APP_NAME).sandbox(
        name="issue",
        image=Image.from_id(image_id),
        cpu=1,
        memory="4Gi",
        allow_list=[MODEL_API_RANGE],
        keep_warm_seconds=300,
        preemptible=False,
    )


class SnapshotMissingError(RuntimeError):
    pass


@app.function(
    name="solve-issue",
    image=function_image,
    cpu=0.25,
    memory="512Mi",
    timeout_seconds=AGENT_TIMEOUT_SECONDS + 30 * 60,
    retries=0,
    preemptible=False,
    max_pending_tasks=MAX_WAITING_ISSUES,
    autoscaler=Autoscaler(max_containers=MAX_PARALLEL_ISSUES),
    secrets=[ANTHROPIC_API_KEY.name, GITHUB_TOKEN.name],
)
def solve(job: IssueJob) -> IssueOutcome:
    token = os.environ[GITHUB_TOKEN.name]
    with GitHub(job.repository, token) as github:
        try:
            record = STATE.get(SNAPSHOT_KEY)
            if record is None:
                raise SnapshotMissingError("run `uv run python -m coding_agents.snapshot` first")
            snapshot = RepoSnapshot.model_validate(record)
            with started(issue_sandbox(snapshot.image_id)) as instance:
                return solve_issue(
                    job,
                    instance,
                    snapshot,
                    github,
                    api_key=os.environ[ANTHROPIC_API_KEY.name],
                    token=token,
                )
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"[:1000]
            github.comment(job.number, f"The agent run failed:\n\n```\n{failure}\n```")
            raise
        finally:
            STATE.pop(job.claim_key, None)


def start_issue(job: IssueJob) -> str | None:
    """Claim the issue and start a worker on it; an issue already claimed keeps its run.

    The webhook runs one request at a time in one container, so the check and
    the claim cannot race.
    """
    if job.claim_key in STATE:
        return None
    STATE.set(job.claim_key, job.number, ttl=CLAIM_TTL_SECONDS)
    try:
        return solve.spawn(job).task_id
    except SdkError:
        del STATE[job.claim_key]
        raise


api = create_api(start_issue, secret_name=GITHUB_WEBHOOK_SECRET.name)
# GitHub cannot send a LazyCloud token, so the route is public and every
# delivery must carry the webhook secret's signature.
webhook = app.asgi(
    name="webhook",
    image=function_image,
    cpu=0.25,
    memory="256Mi",
    timeout_seconds=30,
    keep_warm_seconds=300,
    authorized=False,
    secrets=[GITHUB_WEBHOOK_SECRET.name],
)(api)


@app.function(
    name="eval-task",
    image=function_image,
    cpu=0.25,
    memory="256Mi",
    timeout_seconds=AGENT_TIMEOUT_SECONDS + 15 * 60,
    retries=0,
    preemptible=False,
    autoscaler=Autoscaler(max_containers=len(task_names())),
    secrets=[ANTHROPIC_API_KEY.name],
)
def eval_task(name: str) -> EvalResult:
    with started(eval_sandbox) as instance:
        return run_eval_task(instance, name, api_key=os.environ[ANTHROPIC_API_KEY.name])


@app.function(
    name="nightly-eval",
    cron="0 6 * * *",
    image=function_image,
    cpu=0.125,
    memory="256Mi",
    timeout_seconds=AGENT_TIMEOUT_SECONDS + 30 * 60,
    retries=0,
    preemptible=False,
    secrets=[GITHUB_TOKEN.name],
)
def nightly_eval() -> EvalReport:
    names = task_names()
    calls = eval_task.spawn_map([(name,) for name in names])
    results: list[EvalResult] = []
    for name, call in zip(names, calls, strict=True):
        outcome = call.result(wait=True)
        if outcome.value is None:
            results.append(EvalResult(task=name, passed=False, error=outcome.error))
        else:
            results.append(outcome.value)
    report = EvalReport(day=datetime.now(UTC).date(), results=results)
    print(report.markdown(), flush=True)
    if EVAL_ISSUE is not None:
        with GitHub(REPOSITORY, os.environ[GITHUB_TOKEN.name]) as github:
            github.comment(EVAL_ISSUE, report.markdown())
    return report
