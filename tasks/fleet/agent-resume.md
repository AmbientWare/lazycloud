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

- Branched from origin/fleet-capacity-plan, rebased onto the provider
  packet's schema commit `325b25cd86d077e50b34aacd7c49d9894c41ef7f` on
  origin/fleet-provider; after the review, rebased onto
  origin/fleet-capacity-plan at `37911167`, with the provider merged.
- Agent: suspend.go (timerfd clock jumps, sleep gap, resume reconnect),
  reserve.go (PrepareReserve), Hello fields 72-73, proto fields 71.
- Compute and host session: migration 0004, reserve_session.go and .sql,
  the OpenSession hook, PrepareReserve sync and ReserveReady routing.

## Intentional differences

- There is no worker process: a hibernated reserve keeps the agent, Docker
  and pulled images, which is what makes its resume fast.
- The reference's update cancellation before a stop becomes a refusal while
  an update is in flight, because agents update in place.
- Sleep attempts are columns on the host (`sleep_attempt_id`,
  `sleep_boot_id`), not a table. The PrepareReserve awaiting an answer
  lives in its session: a reconnect or a 2-minute silence sends a fresh
  attempt, and only the newest answer counts. ReserveReady is the agent's
  marker acknowledgment, so the reference's separate observation ack is
  gone; settling clears the attempt, so a repeated Hello records nothing.
- The slept time comes from the gap growth since the attempt was prepared,
  stored with the attempt, so an agent restart in the same boot still
  reports it, and no sleep is ever counted twice.
- A resume closes the sockets under the three server connections, resets
  their backoff and cancels the session, so nothing waits for TCP to notice
  dead peers. The agent has no interruption shutdown timer to rearm (the
  server acts on the notice); after a resume it drops a Spot notice whose
  reclaim time passed and reads IMDS at once.
- A reserve's release proof is the release the host should run: the target
  once the rollout reaches the host, any release outside it.
- The planner writes `resuming` with `resume_requested_at`, so a lagging
  resume cannot occur; a stopping or stopped host that comes back with a
  sleep or a new boot was not asked to, and goes through resuming and
  joining to preparing, never ready. The session clears
  `resume_requested_at` when a resume joins.
- Activation samples are measured on the database clock: provision from
  the host row's creation to its first session, resume and boot from
  `resume_requested_at` to the Hello. Rows older than a day are pruned on
  insert.
- An agent update never sends a reserve into service. The session sends no
  PrepareReserve while an update is offered or `updating_until` is set,
  and a refusal marked `ReserveReady.updating` (a trial not yet committed)
  keeps the host preparing and is asked again on a later sync. plan.md's
  "preparing -> ready (agent refused: work arrived, update in flight)"
  becomes "work arrived"; an update in flight keeps the host preparing.
- A host the planner resumes while it is still stopping never slept and
  sends no new Hello, so its session ends with Aborted when it finds the
  host resuming; the agent's next Hello settles the resume.
- A resume that joins also ends the last stop's facts
  (`stop_requested_at`, `force_stop_at`, `hibernate_refused_at`,
  `stopped_at`), even when the actuator never recorded the start.
- P1 (approved), in its own commit: the agent writes no kmsg marker, and
  the resume report proves the hibernation. Settling a resume writes
  `image_evidence`: memory restored makes it `saved`, a cold boot after a
  `saved` hibernation makes it `failed`. The day-long unreliability mark for
  that type and region is the `fleet_activations` row with kind `resume`
  and outcome `cold_boot`; the planner's activation query reads it.

## Evidence

Parity lines, with the tests that prove them (agent: internal/agent,
session: internal/hostsession against real PostgreSQL):

- Sleep detection from the CLOCK_BOOTTIME and CLOCK_MONOTONIC gap, woken by
  a timerfd clock jump, start fails without it: agent
  TestKernelClockWaitsForAJumpUntilCancelled,
  TestSleptSinceCountsOnlyTheAttemptsBoot,
  TestAgentReconnectsAndReportsTheSleepAfterAResume.
- Rearm after a sleep from the wall clock: agent
  TestResumeRearmsTheSpotNoticeFromTheWallClock.
- Sleep report with attempt and boot on the next session: agent
  TestAgentReconnectsAndReportsTheSleepAfterAResume,
  TestAgentRemembersTheSleepAttemptAcrossRestarts.
- Current agent release, GPU driver proof, no live containers for that exact
  request, no update in flight: session TestReserveStopsOnlyOnTheAgentsProof,
  TestReserveRefusesUnprovenHosts, TestGPUReserveRecordsItsProof; agent
  TestAgentRefusesTheReserveWhileWorkRemains,
  TestReserveBlockerRefusesDuringAnUpdate.
- Attempts fenced by boot, superseded attempts stale: session
  TestReserveStopsOnlyOnTheAgentsProof, TestReserveSupersededAttemptIsStale.
- A resume serves only when requested; an unrequested wake stays out of
  placement and stops again: session TestUnrequestedWakeGoesBackToTheReserve.
- Resume outcome, activation samples, refresh prepares again, joins before
  ready: session TestResumeSettlesTheSleepAttempt,
  TestFirstSessionOfALaunchedHost.

Resume to reconnect: 1.8 ms from the clock jump to the server's next
session in the agent harness (fake clock, local server). Not measured on
EC2; see gaps.

Checks: `go test -race ./internal/agent ./internal/hostsession
./internal/compute` pass; `go generate ./...` leaves the tree clean;
golangci-lint over ./... has 0 issues once the provider's
`Propose:` fleet_admin.go switch commit (9b2fbc49) is applied, and 1
(that switch) on the schema commit alone; go vet, buf format and buf lint
pass. acceptance/neki/check.sh: 0 router failures with 0003 and 0004
applied through the router. check-neki.sh against prod's schema lists only
the columns 0003 and 0004 add.

## Gaps and unverified boundaries

- The real EC2 hibernation run did not happen. `default-test` assumes
  `lazycloud-default-test-operator` in account 534742592531, the same
  account `default` (root) and the prod fleet use (it can see terminated
  `lazycloud:fleet = lazycloud-prod` instances), so it is not a separate
  disposable account. The role is also denied ec2:DescribeSubnets,
  DescribeImages, DescribeVpcs, ssm:GetParameters and s3:CreateBucket, so
  no AMI, subnet or presigned binary is reachable from it. Nothing was
  created. The timerfd wake on a real resume, and the delay from EC2 start
  to agent reconnect, remain unmeasured.
- The 2-minute supersede timer itself is untested (the reconnect path that
  also sends a fresh attempt is).
- The planner's return to the reserve should notify the host channel so the
  session sends PrepareReserve at once; otherwise it waits for the next
  touch (10 s).

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
