# Packet brief

Every owner agent reads this, AGENTS.md, update.md, tasks/parity.md,
tasks/function-execution.md (the working slice and its conventions) and
tasks/wave-2.md (designs and shared-file rules) before planning.

## Goal

Your packet delivers its parity sections from tasks/parity.md one to one: the
same SDK API, CLI commands and flags, output, URLs and documented behavior as
the reference, implemented better in Go. A missing command or option is a gap,
never a simplification. Record each intentional user-visible difference and its
reason in your task file, tasks/<packet>.md, and write your plan, progress and
evidence there too.

## Environment

- Go: the system Go is 1.22, so always `export GOTOOLCHAIN=go1.27.1`. Tools run
  as `go tool <name>`: sqlc, oapi-codegen, buf, golangci-lint. `go generate
  ./...` regenerates every binding; commit generated files.
- New dependencies take the latest release as of today, pinned. Keep them few.
- Python: `~/.local/bin/uv` from the repo root. Run `uv sync --group dev`,
  `uv run --group dev pytest -x <paths>` and ruff. Bindings regenerate with
  `uv run --group dev datamodel-codegen --profile api` and `--profile runner`.
  The SDK and runner support Python 3.10+.
- Test PostgreSQL: `docker compose -f compose.test.yaml up -d --wait`. Other
  agents share it, so never stop it. `dbtest.New(t)` gives each test its own
  database.
- Local platform: `deploy/local/run.sh start` builds and runs server, scheduler
  and agent against compose.yaml services and prints the SDK environment.
  Other agents may run it too. For an end-to-end check use your own copy:
  change ports, the compose project name and the `.lazycloud` state directory
  in your worktree so stacks do not collide. Never touch containers or compose
  projects you did not create.
- Docker is available with runc. gVisor is not installed locally.
- Reference (read-only; never modify; never read its .env or credentials):
  /home/cmclean/.t3/worktrees/lazycloud/t3code-9782b237 at 9e259ce75. Use it
  for behavior, not structure.

## Rules

- Owners: Go code goes in the packages named by your packet. Use
  `Execution.finishAttempt` and `Execution.containerExited` for attempt and
  container outcomes; never write a second transition. The lock order is
  container, then task, then attempt.
- Shared files follow tasks/wave-2.md: your numbered migration only, your
  protobuf field range, your OpenAPI paths, minimal wiring edits.
- Containers hold no platform credential. In-container calls go through the
  container API once the workload-runtime packet provides it.
- Tests run against real PostgreSQL, real Docker and a real object store at
  owner boundaries, with no mocks of owned boundaries. Cover the failure,
  retry and cleanup paths. Run gofmt, go vet, `go tool golangci-lint run
  ./...` and `go test -race` on your packages, and pytest -x on Python you
  touch.
- Measure what your area makes users wait for, and record it in your task file.
- Commits: one-line subject, no body, no attribution trailers. Commit and
  `git push -u origin <branch>` after each meaningful step so work survives a
  restart. Do not open PRs; the integrator merges.
- If you must change something another packet owns, make the smallest change
  in a separate commit titled `Propose: ...` and explain it in your report.

## Report

Under 600 words: parity items delivered, with test names; measurements;
proposed shared changes; remaining gaps; how to try the feature locally.
