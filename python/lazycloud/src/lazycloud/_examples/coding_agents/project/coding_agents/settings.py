"""What to point the system at and how far an agent may go. Edit before deploying."""

from __future__ import annotations

# The GitHub repository the agents work on, as owner/name.
REPOSITORY = "your-org/your-repo"
# Adding this label to an issue hands the issue to an agent.
TRIGGER_LABEL = "agent"
# Nightly eval scores are posted as a comment on this issue; None only logs them.
EVAL_ISSUE: int | None = None

# Run inside the repository as the agent user. Setup runs once, when the
# snapshot is built with network access; tests run offline inside the sandbox.
SETUP_COMMAND = "uv sync --locked"
TEST_COMMAND = "uv run --offline --locked pytest -q"

# Claude Code settings for every agent run.
AGENT_MODEL = "sonnet"
AGENT_MAX_TURNS = 60
AGENT_MAX_BUDGET_USD = 5.0
AGENT_TIMEOUT_SECONDS = 30 * 60
TEST_TIMEOUT_SECONDS = 10 * 60

# Issue runs at once, and issue runs waiting for a slot before new ones are refused.
MAX_PARALLEL_ISSUES = 3
MAX_WAITING_ISSUES = 20
# Longest issue body handed to the agent; GitHub allows 65,536 characters.
MAX_ISSUE_CHARS = 20_000
# Largest change an agent may propose in one pull request.
MAX_PATCH_BYTES = 1_000_000

COMMIT_AUTHOR = "coding-agents"
COMMIT_EMAIL = "coding-agents@users.noreply.github.com"
