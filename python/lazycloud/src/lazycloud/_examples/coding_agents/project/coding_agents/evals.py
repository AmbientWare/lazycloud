"""Score the agent on small tasks whose hidden checks it never sees."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from lazycloud import SandboxInstance
from pydantic import BaseModel

from coding_agents.agent import (
    AGENT_ENV,
    AGENT_HOME,
    as_agent,
    pin_model_api,
    run_agent,
    run_checked,
)

TASKS = Path(__file__).parents[1] / "eval_tasks"
TASK_DIR = f"{AGENT_HOME}/task"
HIDDEN_CHECKS = "hidden_checks.py"

EVAL_PROMPT = """\
{instructions}
Work in the current directory. `python -m pytest -q checks.py` runs the checks \
written so far; add your own as you go. The network reaches only the model API.
"""


class EvalResult(BaseModel):
    task: str
    passed: bool
    agent_cost_usd: float | None = None
    agent_turns: int | None = None
    seconds: float | None = None
    error: str | None = None


class EvalReport(BaseModel):
    day: date
    results: list[EvalResult]

    @property
    def pass_rate(self) -> float:
        return sum(result.passed for result in self.results) / len(self.results)

    @property
    def agent_cost_usd(self) -> float:
        return sum(result.agent_cost_usd or 0.0 for result in self.results)

    def markdown(self) -> str:
        rows = [
            f"| {r.task} | {'pass' if r.passed else 'fail'} | "
            f"{'' if r.agent_cost_usd is None else f'${r.agent_cost_usd:.2f}'} | "
            f"{'' if r.agent_turns is None else r.agent_turns} | "
            f"{'' if r.seconds is None else f'{r.seconds:.0f}s'} | {r.error or ''} |"
            for r in self.results
        ]
        return "\n".join(
            [
                f"Agent eval {self.day.isoformat()}: {self.pass_rate:.0%} passed, "
                f"${self.agent_cost_usd:.2f} model spend.",
                "",
                "| Task | Result | Model spend | Turns | Time | Error |",
                "| --- | --- | --- | --- | --- | --- |",
                *rows,
            ]
        )


def task_names() -> list[str]:
    return sorted(path.name for path in TASKS.iterdir() if (path / "task.md").is_file())


def run_eval_task(instance: SandboxInstance, name: str, *, api_key: str) -> EvalResult:
    """Give the agent one task, then score its work with checks it could not read."""
    task = TASKS / name
    began = time.monotonic()
    pin_model_api(instance)
    for source in sorted((task / "workspace").iterdir()):
        instance.fs.upload_file(source, f"{TASK_DIR}/{source.name}")
    run_checked(instance, ["chown", "-R", "agent:agent", TASK_DIR], cwd="/", timeout_seconds=30)
    prompt = EVAL_PROMPT.format(instructions=(task / "task.md").read_text(encoding="utf-8"))
    agent = run_agent(instance, cwd=TASK_DIR, prompt=prompt, api_key=api_key)
    # Uploaded after the agent stops; --noconftest keeps a conftest.py the
    # agent wrote from changing how the hidden checks run.
    instance.fs.upload_file(task / HIDDEN_CHECKS, f"{TASK_DIR}/{HIDDEN_CHECKS}")
    checks = instance.run(
        as_agent(
            "python", "-m", "pytest", "-q", "--noconftest", "-p", "no:cacheprovider", HIDDEN_CHECKS
        ),
        cwd=TASK_DIR,
        env=AGENT_ENV,
        timeout_seconds=120,
    )
    return EvalResult(
        task=name,
        passed=checks.exit_code == 0,
        agent_cost_usd=agent.total_cost_usd,
        agent_turns=agent.num_turns,
        seconds=time.monotonic() - began,
    )
