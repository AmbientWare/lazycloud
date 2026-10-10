from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lazycloud.exceptions import SdkError
from pydantic import ValidationError

import lazycloud

sys.path.insert(0, str(Path(lazycloud.__file__).parent / "_examples" / "coding_agents" / "project"))

from coding_agents.agent import (
    MODEL_API_RANGE,
    AgentOutputError,
    ModelApiAddressError,
    address_in_range,
    parse_agent_output,
)
from coding_agents.evals import HIDDEN_CHECKS, TASKS, EvalReport, EvalResult, task_names
from coding_agents.github import push_patch
from coding_agents.github_events import IssueJob, issue_job, signature_matches
from coding_agents.settings import MAX_ISSUE_CHARS, REPOSITORY, TRIGGER_LABEL
from coding_agents.webhook import MAX_DELIVERY_BYTES, create_api

SECRET = b"webhook-secret"


def labeled(label: str = TRIGGER_LABEL, repository: str = REPOSITORY, body: str = "") -> bytes:
    return json.dumps(
        {
            "action": "labeled",
            "label": {"name": label},
            "repository": {"full_name": repository},
            "issue": {
                "number": 7,
                "title": "Crash on empty input",
                "body": body,
                "html_url": f"https://github.com/{repository}/issues/7",
            },
        }
    ).encode()


def signed(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_only_the_exact_body_under_the_shared_secret_verifies() -> None:
    body = labeled()
    assert signature_matches(SECRET, body, signed(body))
    assert not signature_matches(SECRET, body + b" ", signed(body))
    assert not signature_matches(SECRET, body, signed(body, b"other-secret"))
    assert not signature_matches(SECRET, body, None)
    assert not signature_matches(SECRET, body, "sha256=é")


def test_only_the_trigger_label_on_the_configured_repository_starts_a_job() -> None:
    job = issue_job("issues", labeled(body="Steps to reproduce"))
    assert job == IssueJob(
        repository=REPOSITORY,
        number=7,
        title="Crash on empty input",
        body="Steps to reproduce",
    )
    assert issue_job("issues", labeled(label="bug")) is None
    assert issue_job("issues", labeled(repository="someone/else")) is None
    assert issue_job("issue_comment", labeled()) is None
    with pytest.raises(ValidationError):
        issue_job("issues", b'{"action": "labeled"}')


def test_long_issues_are_cut_to_the_agent_limit() -> None:
    job = issue_job("issues", labeled(body="x" * (MAX_ISSUE_CHARS + 500)))
    assert job is not None
    assert job.body.startswith("x" * MAX_ISSUE_CHARS)
    assert "x" * (MAX_ISSUE_CHARS + 1) not in job.body


@pytest.fixture
def webhook(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, list[IssueJob]]:
    monkeypatch.setenv("TEST_WEBHOOK_SECRET", SECRET.decode())
    started: list[IssueJob] = []

    def start(job: IssueJob) -> str | None:
        if any(seen.number == job.number for seen in started):
            return None
        started.append(job)
        return "task-1"

    return TestClient(create_api(start, secret_name="TEST_WEBHOOK_SECRET")), started


def deliver(
    client: TestClient, event: str, body: bytes, signature: str | None
) -> dict[str, object]:
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    response = client.post("/github", content=body, headers=headers)
    return {"status_code": response.status_code, **response.json()}


def test_unsigned_or_missigned_deliveries_start_nothing(
    webhook: tuple[TestClient, list[IssueJob]],
) -> None:
    client, started = webhook
    body = labeled()
    assert deliver(client, "issues", body, None)["status_code"] == 401
    assert deliver(client, "issues", body, signed(body, b"guess"))["status_code"] == 401
    assert started == []


def test_a_labeled_issue_starts_one_run(webhook: tuple[TestClient, list[IssueJob]]) -> None:
    client, started = webhook
    body = labeled()
    assert deliver(client, "ping", b"{}", signed(b"{}"))["status"] == "pong"
    assert deliver(client, "issues", body, signed(body)) == {
        "status_code": 200,
        "status": "started",
        "task_id": "task-1",
    }
    assert deliver(client, "issues", body, signed(body))["status"] == "already_running"
    other = labeled(label="bug")
    assert deliver(client, "issues", other, signed(other))["status"] == "ignored"
    assert [job.number for job in started] == [7]


def test_oversized_and_refused_deliveries_get_errors_github_shows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_WEBHOOK_SECRET", SECRET.decode())

    def refuse(job: IssueJob) -> str | None:
        raise SdkError("too many pending tasks")

    client = TestClient(create_api(refuse, secret_name="TEST_WEBHOOK_SECRET"))
    body = labeled()
    assert deliver(client, "issues", body, signed(body))["status_code"] == 503
    huge = labeled(body="x" * MAX_DELIVERY_BYTES)
    assert deliver(client, "issues", huge, signed(huge))["status_code"] == 413


def test_agent_results_parse_even_when_a_limit_stops_the_run() -> None:
    stopped = json.dumps(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "num_turns": 60,
            "total_cost_usd": 1.25,
            "session_id": "abc",
        }
    )
    run = parse_agent_output(stopped + "\n", "", 1)
    assert (run.subtype, run.num_turns, run.total_cost_usd, run.result) == (
        "error_max_turns",
        60,
        1.25,
        "",
    )
    with pytest.raises(AgentOutputError, match="Invalid API key"):
        parse_agent_output("", "Invalid API key", 1)
    rejected = json.dumps(
        {
            "subtype": "success",
            "is_error": True,
            "result": "Invalid API key",
            "num_turns": 1,
            "total_cost_usd": 0,
        }
    )
    with pytest.raises(AgentOutputError, match="Invalid API key"):
        parse_agent_output(rejected, "", 1)


def test_the_pinned_model_api_address_is_inside_the_allow_list() -> None:
    assert address_in_range(["203.0.113.5", "160.79.104.10"], MODEL_API_RANGE) == "160.79.104.10"
    with pytest.raises(ModelApiAddressError):
        address_in_range(["203.0.113.5"], MODEL_API_RANGE)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_a_patch_lands_on_the_issue_branch_and_a_rerun_replaces_it(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--quiet", "--bare", str(remote))
    clone = tmp_path / "clone"
    git(tmp_path, "init", "--quiet", str(clone))
    (clone / "app.py").write_text("print('hello')\n")
    (clone / "old.txt").write_text("remove me\n")
    git(clone, "add", "--all")
    git(clone, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base")
    base = git(clone, "rev-parse", "HEAD")
    git(clone, "push", "--quiet", str(remote), "HEAD:refs/heads/main")

    def patch_from(change: str, name: str) -> Path:
        (clone / "app.py").write_text(change)
        (clone / "old.txt").unlink(missing_ok=True)
        (clone / "logo.bin").write_bytes(bytes(range(256)))
        git(clone, "add", "--all")
        patch = tmp_path / name
        patch.write_text(git(clone, "diff", "--cached", "--binary", base) + "\n")
        git(clone, "reset", "--quiet", "--hard", base)
        return patch

    for attempt, change in enumerate(["print('fixed')\n", "print('fixed again')\n"]):
        workdir = tmp_path / f"work-{attempt}"
        workdir.mkdir()
        commit = push_patch(
            remote=str(remote),
            base_commit=base,
            branch="agent/issue-7",
            patch=patch_from(change, f"{attempt}.patch"),
            message="Fix the crash\n\nCloses #7",
            workdir=workdir,
            env={},
        )
        assert git(remote, "rev-parse", "agent/issue-7") == commit
        assert git(remote, "rev-parse", "agent/issue-7^") == base
        assert git(remote, "show", "agent/issue-7:app.py") == change.strip()
        assert git(remote, "ls-tree", "--name-only", "agent/issue-7").split() == [
            "app.py",
            "logo.bin",
        ]


REFERENCE_SOLUTIONS = {
    "csv_totals": (
        "report.py",
        "import csv\nimport io\nfrom decimal import Decimal\n\n\n"
        "def totals_by_category(csv_text):\n"
        "    totals = {}\n"
        "    for row in csv.DictReader(io.StringIO(csv_text)):\n"
        "        totals[row['category']] = totals.get(row['category'], Decimal(0))"
        " + Decimal(row['amount'])\n"
        "    return totals\n",
    ),
    "lru_cache": (
        "cache.py",
        "from collections import OrderedDict\n\n\n"
        "class LRUCache:\n"
        "    def __init__(self, capacity):\n"
        "        self.capacity, self.items = capacity, OrderedDict()\n\n"
        "    def get(self, key):\n"
        "        if key not in self.items:\n"
        "            return None\n"
        "        self.items.move_to_end(key)\n"
        "        return self.items[key]\n\n"
        "    def put(self, key, value):\n"
        "        self.items[key] = value\n"
        "        self.items.move_to_end(key)\n"
        "        if len(self.items) > self.capacity:\n"
        "            self.items.popitem(last=False)\n",
    ),
    "page_count": (
        "pagination.py",
        "def page_count(total, per_page):\n"
        "    if per_page < 1:\n"
        "        raise ValueError(per_page)\n"
        "    return -(-total // per_page)\n",
    ),
    "parse_duration": (
        "durations.py",
        "import re\n\n\n"
        "def parse_duration(text):\n"
        "    match = re.fullmatch(r'(?:(\\d+)h)?(?:(\\d+)m)?(?:(\\d+)s)?', text)\n"
        "    if not text or match is None:\n"
        "        raise ValueError(text)\n"
        "    h, m, s = (int(part or 0) for part in match.groups())\n"
        "    return h * 3600 + m * 60 + s\n",
    ),
    "slugify": (
        "slug.py",
        "import re\nimport unicodedata\n\n\n"
        "def slugify(title):\n"
        "    plain = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode()\n"
        "    return re.sub(r'[^a-z0-9]+', '-', plain.lower()).strip('-')\n",
    ),
}


def checks_pass(workspace: Path, *files: str) -> bool:
    command = [sys.executable, "-m", "pytest", "-q", "--noconftest", "-p", "no:cacheprovider"]
    done = subprocess.run([*command, *files], cwd=workspace, capture_output=True, check=False)
    return done.returncode == 0


@pytest.mark.parametrize("name", task_names())
def test_hidden_checks_fail_the_starting_code_and_pass_a_correct_solution(
    name: str, tmp_path: Path
) -> None:
    workspace = tmp_path / name
    shutil.copytree(TASKS / name / "workspace", workspace)
    shutil.copy(TASKS / name / HIDDEN_CHECKS, workspace)
    assert not checks_pass(workspace, HIDDEN_CHECKS)
    module, source = REFERENCE_SOLUTIONS[name]
    (workspace / module).write_text(source)
    assert checks_pass(workspace, "checks.py", HIDDEN_CHECKS)


def test_the_report_counts_failed_calls_without_inventing_their_cost() -> None:
    report = EvalReport(
        day=date(2026, 10, 9),
        results=[
            EvalResult(task="a", passed=True, agent_cost_usd=0.4, agent_turns=9, seconds=80),
            EvalResult(task="b", passed=False, agent_cost_usd=0.35, agent_turns=12, seconds=95),
            EvalResult(task="c", passed=False, error="SandboxConnectionError: no capacity"),
        ],
    )
    assert report.pass_rate == pytest.approx(1 / 3)
    assert report.agent_cost_usd == pytest.approx(0.75)
    assert (
        report.markdown().splitlines()[0] == "Agent eval 2026-10-09: 33% passed, $0.75 model spend."
    )
    assert (
        report.markdown().splitlines()[-1]
        == "| c | fail |  |  |  | SandboxConnectionError: no capacity |"
    )
