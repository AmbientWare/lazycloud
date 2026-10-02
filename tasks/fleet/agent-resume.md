# Agent resume and reserve sessions

## Scope

The host-protocol half of reserves: the agent notices a resume, reconnects
at once and reports it; before a stop it proves it is ready; compute's
session handlers move reserve phases and record activation samples.

Parity sections (tasks/fleet/parity.md): Agent; the proof, cleanup,
sleep-attempt, resume-authorization and resume-outcome lines in Reserve
lifecycle; activation samples in Forecast and timing.

Owns:

- `contracts/host/v1/host.proto`, compute's range 70-79:
  - `Hello.slept_seconds = 72` (double) and `Hello.sleep_attempt_id = 73`.
  - `ServerMessage.prepare_reserve = 71`: `PrepareReserve {request_id,
    attempt_id, mode (stop or hibernate), gpus}`.
  - `HostMessage.reserve_ready = 71`: `ReserveReady {request_id, attempt_id,
    boot_id, agent_version, gpus, refused (reason, empty when ready)}`.
  - Regenerated `internal/hostproto`.
- `internal/agent/suspend.go` (new): sleep detection and the resume wake.
- `internal/agent/reserve.go` (new): `PrepareReserve` handling.
- Edits in `internal/agent/agent.go`, `session.go` and `cloud.go`: reconnect
  on resume, `Hello` fields, rearm the Spot notice watcher and any deadline
  timers from the wall clock after a sleep.
- `internal/hostsession/compute.go`: send `PrepareReserve` to a host in
  `preparing`, route `ReserveReady`, pass the new `Hello` fields.
- `internal/compute/reserve_session.go` (new) and
  `queries/reserve_session.sql` (new), plus the smallest hook in
  `enrollment.go` where a session opens.
- `migrations/0004_fleet_host_protocol.sql`: `hosts.sleep_attempt_id`,
  `sleep_boot_id`, `prepared_agent_version`, `gpu_proven`,
  `last_resume_outcome`; table `fleet_activations` with an index on `at`.

Migration: 0004. Protobuf: the fields above.

Depends on: the provider packet's first two commits (migration 0003 and the
phase table). Branch from them; rebase on fleet-capacity-plan once the
provider packet merges.

## Plan

1. Suspend detection in Go without cgo: read `CLOCK_BOOTTIME` and
   `CLOCK_MONOTONIC` through golang.org/x/sys/unix; a gap growth over 1 s is
   a sleep (suspend.py:23, 33-47). Wake on a `TFD_TIMER_CANCEL_ON_SET`
   timerfd, as the reference did (suspend.py:58-92), or on a 1 s ticker if
   that proves enough; measure the delay from resume to reconnect and keep
   the cheaper one that reconnects within 2 s. On Linux the agent fails to
   start without the notice.
2. On a sleep: cancel the session so it reconnects now, rearm the
   interruption watcher and any wall-clock deadlines (capacity_shutdown.py:
   43-47), and send `Hello` with `slept_seconds` and the attempt it slept
   under.
3. `PrepareReserve`: refuse while any container exists, a disk or volume is
   mounted, or an agent update is in flight; for a GPU reserve, report the
   cards found through the driver; for `hibernate`, write
   `lazycloud-sleep attempt=<id> boot=<boot>` to /dev/kmsg; answer
   `ReserveReady`. The agent keeps Docker and its image cache.
4. Compute handlers. On `ReserveReady` for a host in `preparing` with the
   same request and boot: agent on the target release, GPU proof when the
   host has GPUs (enrollment GPU count and preflight passed), zero live
   containers, then move to `stopping` and set `sleep_attempt_id`. A refusal
   returns the host to `ready`. A fresh attempt supersedes one that saw no
   marker within 2 minutes (sleep_lifecycle.py:58-102).
5. On `Hello` from a host in `resuming`, or a lagging `stopping` or `stopped`
   host whose resume was requested: move to `joining`; the first session
   makes it `ready`. Record `last_resume_outcome` (same boot id and slept
   seconds: memory restored; new boot id: cold boot) and a
   `fleet_activations` sample. A `Hello` from a reserve whose resume nobody
   requested keeps it out of placement and asks the planner to stop it again
   (machine_lifecycle.py:119-137). The first session of a newly launched
   host records a provision sample.

## Progress

## Intentional differences

- There is no worker process: a hibernated reserve keeps the agent, Docker
  and pulled images, which is what makes its resume fast.
- The reference's update cancellation before a stop becomes a refusal while
  an update is in flight, because agents update in place.

## Evidence

## Gaps and unverified boundaries

## Verification

- Agent unit tests with an injected clock source for the sleep gap, and
  agent tests in the existing harness for `PrepareReserve` refusals.
- hostsession and compute tests with real PostgreSQL for every transition,
  stale request ids, superseded attempts, lagging resumes and an unrequested
  wake.
- `go test -race ./internal/agent ./internal/hostsession ./internal/compute`,
  `go generate ./...` with a clean tree after, gofmt, go vet, golangci-lint,
  `./check.sh`. depguard must still keep the agent off the database.
- Real EC2 in `AWS_PROFILE=default-test`, us-east-2, after `aws sts
  get-caller-identity --profile default-test` confirms the disposable
  account. Build the agent for linux/amd64, serve it to one m7i.large able to
  hibernate through a presigned URL from a bucket you create, run it pointed
  at an unreachable server, hibernate the instance, start it, and read from
  console output the time the agent logged the resume and the slept seconds
  it measured. Delete the instance and the bucket afterwards.

## Brief

```text
You own the agent-resume packet of the fleet capacity work for LazyCloud
(repo github.com/AmbientWare/lazycloud). Work alone; do not start
sub-agents. Create branch `fleet-agent-resume` from the provider packet's
schema commit on origin/fleet-provider (ask the integrator for the SHA) and
record it in tasks/fleet/agent-resume.md.

Read first: AGENTS.md, tasks/fleet/README.md, tasks/fleet/plan.md (the
intent table and state machine are your contract), tasks/fleet/parity.md,
tasks/fleet/agent-resume.md. The reference is commit 9e259ce75: read it with
`git show 9e259ce75:<path>` for behavior only; never read its env or
credentials. Start with packages/agent/src/agent/suspend.py,
capacity_shutdown.py, packages/compute/src/compute/reserve_machines.py
(lines 116-475), sleep_lifecycle.py and machine_lifecycle.py.

Goal: deliver the parity lines marked "Packet: agent-resume" one to one,
implemented better: no cgo, every goroutine owned and stopped, facts on the
host row instead of separate attempt tables. Record differences in
tasks/fleet/agent-resume.md.

You own the files under "Owns" in tasks/fleet/agent-resume.md, protobuf
fields 71-73 as listed, migration 0004. Stay off launcher.go, reconcile.go,
the actuator, capacity_controller.go, offers.go, the planning queries,
internal/api and web/. A change elsewhere is a `Propose: ...` commit,
explained in your report.

Environment: go1.27.1 (export GOTOOLCHAIN=go1.27.1 if needed); `go generate
./...` regenerates protobuf and sqlc bindings; commit them. Test PostgreSQL:
`docker compose -f compose.test.yaml up -d --wait`; shared, never stop it.

Verify: the tests and the real EC2 run under Verification in
tasks/fleet/agent-resume.md, in AWS_PROFILE=default-test only, after sts
get-caller-identity confirms the disposable account. Delete every instance
and bucket you create, and say so in your report.

Commit and push after each meaningful step, one-line subjects, no trailers.
Don't open PRs. Report in under 500 words: parity lines delivered with test
names, resume-to-reconnect delay measured, proposed shared changes, gaps.
```
