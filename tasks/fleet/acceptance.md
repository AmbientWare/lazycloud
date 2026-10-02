# Acceptance and benchmarks

## Scope

The evidence that the integrated fleet meets the bar: cold start by path,
planner pass cost with a growing backlog, price outcomes, idle cost, and the
full path on prod after the Ship. This packet also owns the parity sweep of
tasks/fleet/parity.md.

Parity sections: every line, for the sweep; Simulation and measurement.

Owns:

- `internal/compute/fleet_ec2_matrix_test.go` (new): the cold start matrix on
  real EC2, gated on `LAZYCLOUD_EC2_ACCEPTANCE_PROFILE`.
- `internal/compute/fleet_pass_bench_test.go` (new): pass cost benchmarks
  and a guard test on statement count and buffers.
- `acceptance/fleet_test.go` (new): cross-owner workflow on real PostgreSQL,
  Docker and the managed runtime with a local agent host.
- tasks/fleet/acceptance.md (this file): evidence and the parity sweep.

Migration: none.

Depends on: every other packet merged into fleet-capacity-plan. The prod
steps run after the user's go-ahead for the Ship.

## Plan

1. Cold start matrix in `default-test`, us-east-2: c6a.2xlarge, m7i.xlarge
   and g4dn.xlarge (GPU stops plainly; if the account's G quota is 0, record
   the gap and ask the integrator). For each: launch to running, stop to
   stopped, StartInstances to running for a plain stop, hibernate to stopped
   and StartInstances to running for a hibernated host; 10 runs each, p50 and
   p95. Agent-ready times come from the prod run, since hosts here cannot
   reach a server.
2. Pass cost: 10, 100 and 1,000 hosts by 0, 500, 2,000 and 10,000 pending
   containers, plus a 10-minute arrivals window at 1,000 containers a minute.
   Record wall time, statements, rows and shared buffers per statement; the
   guard test fails if the statement count grows with backlog or buffers grow
   past the batch.
3. Price outcomes: run the policy packet's scenario tests for the rewrite
   today, the reference policy and each proposal the user approved; record
   spend, idle spend, waits over 30 s, p95 capacity wait and churn.
4. Local cross-owner acceptance: a deployed function's pending container
   lands on a ready local host; with no room, the pass records the wait and
   the intent it would act on; the admin API shows the published plan.
5. Prod, after the Ship (user go-ahead): deploy a function with 8 vCPU and
   another with 1 GPU; measure first-request latency split into admission
   and placement, capacity wait, and user execution: from cold (no reserve),
   from a stopped reserve, from a hibernated reserve, and on a packed host.
   Watch the dashboard Fleet page reach its targets and show stopped and
   hibernated nodes; screenshot it. Record the idle cost after an hour at
   zero load from the hosts that exist.
6. Parity sweep: mark every line delivered with test names, intentional with
   its record, or a gap with a size.

## Progress

Branch `fleet-acceptance` from `fleet-capacity-plan` at
`4a8ff51f6fd3d86dba7d5ad6dbc050c3e79dcea3`, every packet merged.

The user narrowed this packet on 2026-10-02, overriding the plan above:
real EC2 runs in `default` (534742592531, the platform account; `default-test`
is the connected-account second account, not a sandbox), at most 3
acceptance instances at once, under an hour; the four gated EC2 tests fold
into `internal/compute/fleet_ec2_acceptance_test.go`; no benchmark files,
screenshots or local cross-owner suite (the planner's pass-cost guard and
emulator end-to-end test cover them); the scheduler role is checked with
`iam simulate-principal-policy`; the Spot snapshot is re-recorded from
DescribeSpotPriceHistory.

## Intentional differences

## Evidence

## Gaps and unverified boundaries

## Verification

- Real EC2 in `AWS_PROFILE=default-test` only, after `aws sts
  get-caller-identity --profile default-test` confirms the disposable
  account. Tag everything `lazycloud:fleet = acceptance`; a cleanup sweeps
  instances and Spot requests with that tag even when a run fails; record
  the sweep's result.
- Benchmarks with `-benchtime` set and visible output; no piping to tail.
- Prod steps use the dashboard and SDK as the user does, and delete the
  apps they deploy afterwards.

## Brief

```text
You own the acceptance packet of the fleet capacity work for LazyCloud (repo
github.com/AmbientWare/lazycloud). Work alone; do not start sub-agents.
Create branch `fleet-acceptance` from origin/fleet-capacity-plan after the
policy, provider, agent-resume, planner and api-web packets merged; record
the SHA in tasks/fleet/acceptance.md.

Read first: AGENTS.md, tasks/fleet/README.md, tasks/fleet/plan.md
("Measurements"), tasks/fleet/parity.md, and every packet file in
tasks/fleet/ for what was delivered.

Goal: measure what plan.md lists, sweep parity.md, and record the evidence
in tasks/fleet/acceptance.md. A regression against the rewrite today is a
bug: find the cause, report it with a proposed fix, and add a guard test.

You own the files under "Owns" in tasks/fleet/acceptance.md. Stay off
production code; a fix is a `Propose: ...` commit, explained in your report.

Environment: go1.27.1 (export GOTOOLCHAIN=go1.27.1 if needed). Test
PostgreSQL: `docker compose -f compose.test.yaml up -d --wait`; shared,
never stop it. Run local stacks only as your own copy and take them down.

Verify: real EC2 in AWS_PROFILE=default-test only, after sts
get-caller-identity confirms the disposable account; clean up every
instance and Spot request. The prod steps wait for the integrator to say the
Ship is done; they touch nothing but the apps you deploy, which you delete.

Commit and push after each meaningful step, one-line subjects, no trailers.
Don't open PRs. Report in under 500 words: the measurements with their
conditions, parity sweep counts, regressions found, cleanup done.
```
