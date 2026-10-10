# Coding agents

A devbox where you work with Claude Code or Codex over SSH. A GitHub webhook
that hands each issue labeled `agent` to Claude Code in its own sandbox, runs
your tests and opens a pull request. A nightly eval that scores the agent on
five small tasks in parallel sandboxes and posts the pass rate and cost.

Set `REPOSITORY` in `coding_agents/settings.py`, then run these commands from
this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run python -m coding_agents.configure
uv run lazycloud deploy coding_agents.app:app
uv run python -m coding_agents.snapshot
```

`configure` stores an Anthropic API key, a GitHub token and a webhook secret
as workspace secrets. Deploy prints the webhook URL; add `<url>/github` as a
webhook on the repository, with content type `application/json`, the same
secret, and the Issues event. `snapshot` clones the repository, runs
`SETUP_COMMAND`, and saves the result as the image issue sandboxes start from.

Label an issue `agent` and a pull request follows. Connect to the devbox with
`uv run lazycloud devbox agent-box ssh`. Run the eval now with
`uv run lazycloud run coding_agents.app:nightly_eval`.

Stop the schedule with `uv run lazycloud deployment stop nightly-eval`. Delete
the app, the `agent-box` disk, the `coding-agents` map and the three secrets
when you no longer need them.

[Full guide](https://docs.lazycloud.dev/examples/coding-agents)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your
code; use `uv sync --locked` in CI. Function images install from the same
lockfile; the agent image pins Node.js, Claude Code, uv and pytest itself.
