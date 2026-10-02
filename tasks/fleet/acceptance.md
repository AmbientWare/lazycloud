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
`4a8ff51f22b29c889875af733360157c26b3a992`, every packet merged; rebased onto
`b8a29020d664d3b40768fd2559d558f7cf2c6a6a` with the planner's follow-ups.

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

- No cold start matrix of 10 runs per type, pass benchmarks, local
  cross-owner suite or screenshots (user, 2026-10-02). The planner's
  `TestPlanningPassCostStaysFlatAsBacklogAndHistoryGrow` and
  `TestAPendingContainerResumesAReserveThatJoinsAndTakesIt` cover pass cost
  and the emulator end to end.
- The real run uses operator credentials in `default`, so the scheduler
  role's permissions come from `iam simulate-principal-policy`, not from
  the run.
- The run launches stock AL2023 and the GPU DLAMI (no `Fleet.Images`):
  prod's baked images predate the hibernation recipe (Gaps).

## Evidence

### Real EC2 (`TestRealEC2Fleet`, internal/compute/fleet_ec2_acceptance_test.go)

`aws sts get-caller-identity --profile default`: account 534742592531.
`LAZYCLOUD_EC2_ACCEPTANCE_PROFILE=default`, us-east-2, default VPC subnets,
stock AL2023 (m7i.large) and the AL2023 GPU DLAMI (g4dn.xlarge), test
PostgreSQL from compose.test.yaml. Each kind runs its cycles in turn
(`LAZYCLOUD_EC2_ACCEPTANCE_RUNS=3`, `..._GPU_RUNS=1`), so at most three
instances run at once.

Runs 2026-10-02 21:16-22:03 UTC (47 minutes of EC2 time). Each duration
is EC2's state polled every 2 s: launch from the launcher's call to
`running`; stop and hibernate from the stop EC2 accepted to `stopped`;
start from the planner's resume to `running`. Guest readiness is not in
these numbers. The m7i.large guests were idle (bootstrap's dnf install
only), so the hibernation image was small.

| Path | Instance | p50 | Runs |
| --- | --- | --- | --- |
| launch to running | m7i.large on-demand | 6 s | 5 (4-6 s) |
| launch to running | m7i.large Spot, persistent | 6 s | 5 (5-7 s) |
| launch to running | g4dn.xlarge on-demand | 9 s | 3 (8-9 s) |
| hibernate to stopped | m7i.large on-demand, 8 GiB | 11 s | 5 (9-18 s) |
| hibernate-resume, start to running | m7i.large on-demand | 5 s | 5 (5 s) |
| plain stop to stopped | m7i.large Spot | 12 s | 5 (9-45 s) |
| plain stop to stopped | g4dn.xlarge, GPU DLAMI | 7 m 27 s | 3 (7 m 10 s-7 m 49 s) |
| start to running | m7i.large Spot | 1 m 27 s | 3 (48 s-1 m 35 s) |
| start to running | g4dn.xlarge | 8 s | 3 (7-8 s) |

A Spot reserve's start includes about 40 s in which EC2 answers
`IncorrectSpotRequestState` while the request turns disabled; the
actuator retries it each pass. One of five Spot starts was refused
(`InsufficientInstanceCapacity`, "no available Spot capacity",
us-east-2c, 21:48 UTC); the actuator retired the reserve, cooled the offer,
cancelled its request and terminated it, as the parity line asks. That is
the case P4 names (stopped Spot reserves that cannot start when the market
is short), measured once.

- Launch: every instance carries `lazycloud:fleet=acceptance`, its host id
  and a Name; the root is an encrypted gp3 of 100 GiB, 108 GiB when it
  hibernates; `HibernationOptions.Configured` is set exactly for the
  hibernating reserve; the Spot reserve's request is persistent,
  interrupts by stopping and carries both tags.
- Hibernate (P1): every hibernation stopped with
  `Client.UserInitiatedHibernate` and the host's evidence `saved`; plain
  stops gave `Client.UserInitiatedShutdown` and `unavailable`.
- Spot reserve: stop, start, then terminate cancelled its persistent request
  and no instance relaunched from it within 30 s.
- Capacity refusals: us-east-2a and later us-east-2c refused m7i.large
  (`InsufficientInstanceCapacity`); the launcher failed and cooled the host
  as designed, and the test moved to the next zone.

Catalog, prices and quotas (P2), read-only in all four regions:

- DescribeInstanceTypeOfferings: every catalog type is priced exactly where
  EC2 sells it. DescribeInstanceTypes: vCPUs, memory and GPU count match
  for all 55 types; the 16 types flagged to hibernate report
  HibernationSupported everywhere they sell. EC2 names the p4d and p4de
  card `A100`; the catalog's `A100-40` and `A100-80` are its own names.
- Per zone, EC2 does not sell every type in every zone of a region:
  use1-az3 offers 2 of 55 catalog types, use1-az5 lacks g6e, p4d and p4de,
  usw2-az4 lacks g4dn, g5 and p4de, and a few zones lack p4de or p5en.
  Prod's fleet networks include use1-az3 and usw2-az4. See Defects.
- `RefreshSpotPrices` stored 473 per-zone quotes (us-east-2 54 types in 3
  zones; a sold type has no quote in a zone exactly where the zone does not
  sell it, plus p5.4xlarge in use2-az3 and p5.48xlarge in usw1-az1).
- `RefreshQuotas` stored all 24 region, class and market quotas; each code
  names the class it is read for (`L-1216C47A` On-Demand Standard,
  `L-34B43A08` Standard Spot, `L-DB2E81BA`/`L-3819A6DF` G and VT,
  `L-417A185B`/`L-7212CCBC` P). Values: standard 512 on-demand and Spot
  everywhere; G on-demand 256 in us-east-2 and 384 elsewhere; G Spot 256 in
  us-east-2 and 128 elsewhere (Oregon's G Spot is no longer 0); P 256-384.

### Scheduler role (`iam simulate-principal-policy`)

Against `arn:aws:iam::534742592531:role/lazycloud-prod-control-plane`
(deployed 2026-10-02 03:40 UTC, older than this branch), alone and with this
branch's fleet statements from deploy/terraform/platform-deployment/iam.tf
added through `--policy-input-list`. 38 cases, 0 mismatches against the
wanted decision with the branch statements.

| Action | Resource | Context | Deployed | Deployed + branch | Want | Use |
| --- | --- | --- | --- | --- | --- | --- |
| ec2:DescribeInstances | * | - | allowed | allowed | allowed | reconcile, actuator |
| ec2:DescribeSpotInstanceRequests | * | - | implicitDeny | allowed | allowed | orphan requests, endSpotRequest |
| ec2:DescribeSpotPriceHistory | * | - | implicitDeny | allowed | allowed | RefreshSpotPrices |
| servicequotas:GetServiceQuota | ec2/L-1216C47A | - | implicitDeny | allowed | allowed | RefreshQuotas |
| ec2:RunInstances | instance/* | lazycloud:fleet=lazycloud-prod | allowed | allowed | allowed | launch, fleet tag |
| ec2:RunInstances | volume/* | lazycloud:fleet=lazycloud-prod | allowed | allowed | allowed | root volume, fleet tag |
| ec2:RunInstances | instance/* | - | implicitDeny | implicitDeny | implicitDeny | launch without the fleet tag |
| ec2:RunInstances | instance/* | lazycloud:fleet=acceptance | implicitDeny | implicitDeny | implicitDeny | launch under another fleet tag |
| ec2:RunInstances | subnet/subnet-0123 | - | allowed | allowed | allowed | fleet subnet |
| ec2:RunInstances | network-interface/* | - | allowed | allowed | allowed | network interface |
| ec2:RunInstances | security-group/sg-0123 | - | allowed | allowed | allowed | security group |
| ec2:RunInstances | spot-instances-request/* | - | allowed | allowed | allowed | Spot request at launch |
| ec2:RunInstances | image/ami-0b1991a9f980b11d1 | - | allowed | allowed | allowed | node image |
| ec2:CreateTags | instance/i-0123456789abcdef0 | ec2:CreateAction=RunInstances | allowed | allowed | allowed | tag instance at launch |
| ec2:CreateTags | volume/vol-0123456789abcdef0 | ec2:CreateAction=RunInstances | allowed | allowed | allowed | tag volume at launch |
| ec2:CreateTags | spot-instances-request/sir-abcdefgh | ec2:CreateAction=RunInstances | implicitDeny | allowed | allowed | tag persistent Spot request at launch |
| ec2:CreateTags | instance/i-0123456789abcdef0 | lazycloud:fleet=lazycloud-prod | implicitDeny | implicitDeny | implicitDeny | retag an instance after launch |
| ec2:StopInstances | instance/i-0123456789abcdef0 | lazycloud:fleet=lazycloud-prod | implicitDeny | allowed | allowed | stop, hibernate, force stop (fleet-tagged) |
| ec2:StopInstances | instance/i-0123456789abcdef0 | - | implicitDeny | implicitDeny | implicitDeny | stop an untagged instance |
| ec2:StartInstances | instance/i-0123456789abcdef0 | lazycloud:fleet=lazycloud-prod | implicitDeny | allowed | allowed | resume (fleet-tagged) |
| ec2:StartInstances | instance/i-0123456789abcdef0 | - | implicitDeny | implicitDeny | implicitDeny | start an untagged instance |
| ec2:TerminateInstances | instance/i-0123456789abcdef0 | lazycloud:fleet=lazycloud-prod | allowed | allowed | allowed | terminate (fleet-tagged) |
| ec2:TerminateInstances | instance/i-0123456789abcdef0 | - | implicitDeny | implicitDeny | implicitDeny | terminate an untagged instance |
| ec2:TerminateInstances | instance/i-0123456789abcdef0 | lazycloud:fleet=acceptance | implicitDeny | implicitDeny | implicitDeny | terminate another fleet's instance |
| ec2:TerminateInstances | * | - | implicitDeny | implicitDeny | implicitDeny | wildcard terminate |
| ec2:CancelSpotInstanceRequests | spot-instances-request/sir-abcdefgh | lazycloud:fleet=lazycloud-prod | implicitDeny | allowed | allowed | cancel a fleet-tagged request |
| ec2:CancelSpotInstanceRequests | spot-instances-request/sir-abcdefgh | - | implicitDeny | implicitDeny | implicitDeny | cancel an untagged request |
| ec2:CancelSpotInstanceRequests | * | - | implicitDeny | implicitDeny | implicitDeny | wildcard cancel |
| ec2:ModifyImageAttribute | image/ami-0b1991a9f980b11d1 | lazycloud:node-image=cpu | allowed | allowed | allowed | share a node image |
| ec2:ModifyImageAttribute | image/ami-0b1991a9f980b11d1 | - | implicitDeny | implicitDeny | implicitDeny | share an untagged image |
| iam:PassRole | role/lazycloud-prod-fleet-node | iam:PassedToService=ec2.amazonaws.com | allowed | allowed | allowed | instance profile |
| iam:PassRole | role/lazycloud-prod-control-plane | iam:PassedToService=ec2.amazonaws.com | implicitDeny | implicitDeny | implicitDeny | pass another role |
| iam:CreateServiceLinkedRole | role/aws-service-role/spot.amazonaws.com/AWSServiceRoleForEC2Spot | iam:AWSServiceName=spot.amazonaws.com | allowed | allowed | allowed | first Spot launch |
| ssm:GetParameters | parameter/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 | - | implicitDeny | implicitDeny | implicitDeny | not called: prod launches baked images |
| ec2:DescribeInstanceTypes | * | - | implicitDeny | implicitDeny | implicitDeny | not called: the catalog is static |
| ec2:DescribeInstanceTypeOfferings | * | - | implicitDeny | allowed | allowed | RefreshZoneOfferings (Propose commit) |
| ec2:GetConsoleOutput | instance/i-0123456789abcdef0 | lazycloud:fleet=lazycloud-prod | implicitDeny | implicitDeny | implicitDeny | not called since P1 |
| pricing:GetProducts | * | - | implicitDeny | implicitDeny | implicitDeny | not called: prices are reviewed in code |

The deployed role lacks every reserve and P2 action (Start, Stop,
CancelSpotInstanceRequests, DescribeSpotInstanceRequests,
DescribeSpotPriceHistory, GetServiceQuota, CreateTags on Spot requests);
the Ship's Terraform apply must land before the fleet code runs.

### Prices

`testdata/fleet/spot_prices.json` re-recorded from DescribeSpotPriceHistory
(Linux/UNIX, latest quote per type and zone, 2026-10-02 21:14 UTC, 473
quotes); the scenario loader now reads its zone ids.
`go test -race -run 'Fleet.*Scenario.*|FleetSpendAtZeroLoad'`, one zone
set in us-east-2, provision 300 s, resume 30 s, boot 120 s:

| Scenario | Policy | $/h | waits > 30 s | p95 wait | launches |
| --- | --- | --- | --- | --- | --- |
| quiet day (24 h) | reference | 0.318 | 0/0 | 0 | 4 |
| quiet day | today | 0.000 | 0/0 | 0 | 0 |
| burst, 40 x 1 vCPU | reference | 0.914 | 31/40 | 5m | 14 |
| burst | today | 0.446 | 40/40 | 5m | 6 |
| scheduled burst | reference | 2.163 | 0/40 | 0 | 15 |
| scheduled burst | today | 0.446 | 40/40 | 5m | 6 |
| reserve depletion | reference | 1.315 | 8/36 | 5m | 19 |
| reserve depletion | today | 0.456 | 12/36 | 5m | 6 |
| GPU burst, 4 x T4 | reference | 1.127 | 4/4 | 5m | 14 |
| GPU burst | today | 0.461 | 4/4 | 5m | 4 |
| 5 x 8 and 3 x 16 vCPU | reference | 1.728 | 8/8 | 5m | 16 |
| 5 x 8 and 3 x 16 vCPU | today | 1.058 | 8/8 | 5m | 5 |
| Oregon G Spot quota 0 (1 h) | quota unknown | 0.418 | n/a | n/a | 60 refused |
| Oregon G Spot quota 0 | P2 | 0.418 | n/a | n/a | 0 refused |

Floors at zero load: $0.3165/h ($231/month), down from $0.326/h on the
public page snapshot. Waits, launches and refusals match the policy
packet's table; spend moved 1-3%, except the Oregon quota scenario
($0.374/h to $0.418/h, from per-zone Oregon prices).

### Defects, each a `Propose:` commit with a test

1. Persistent Spot reserves never launch. EC2 answers
   `InvalidParameterCombination: ... instanceInitiatedShutdownBehavior
   'terminate' is not supported when instanceInterruptionBehavior is set to
   'stop'`, and the launcher fails and cools the offer, so the Spot CPU
   market could never hold a reserve. Fix: persistent Spot launches use
   shutdown behavior `stop` (launcher.go); the AWS fake now refuses the
   combination, so `TestReserveLaunchesHibernateOnAPersistentSpotRequest`
   fails without the fix.
2. On-demand offers in zones that do not sell the type. Offers are made per
   subnet zone, but the catalog knows only regions, and Spot offers are the
   only ones gated by a zone quote. With zone spreading, on-demand buying in
   us-east-1 reaches use1-az3 (2 of 55 types) and GPU buying in us-west-2
   reaches usw2-az4; EC2 refuses with `Unsupported`, which cools the type
   in the whole region for 10 minutes and counts toward region cooling.
   Fix: migration 0007 `fleet_zone_offerings`, an hourly
   `RefreshZoneOfferings` (one DescribeInstanceTypeOfferings per region,
   in the quota loop), `OfferInputs.ZoneTypes` read in the pass, and
   `ec2:DescribeInstanceTypeOfferings` in the role. Tests:
   `TestOffersSkipAZoneThatDoesNotOfferTheType`,
   `TestPurchasesSkipAZoneThatDoesNotOfferTheType` (fails without the
   fix). A region never read limits nothing.

### Parity sweep

tasks/fleet/parity.md: 79 delivered, 8 intentional, 2 gaps.

## Gaps and unverified boundaries

- Node image: prod's `LAZYCLOUD_FLEET_IMAGES` were baked 2026-10-02 14:39
  UTC, before the hibernation recipe (58132dca). Re-bake and update
  deploy/helm/lazycloud/environments/prod.yaml before the Ship, or reserves
  hibernate on an image without hibinit, acpid and the PARTUUID resume.
- The 2-minute supersede timer of a sleep attempt is untested (small).
- g4dn.xlarge on the GPU DLAMI takes 7.5-8 minutes to stop plainly and
  several minutes in shutting-down; the actuator forces a stop after 10.
  The cause is in the guest and is not traced.
- Agent ready, first container, timerfd wake on a real resume, admission
  and placement latency, idle cost and the dashboard against a real plan
  are the prod steps after the Ship.
- `acceptance/neki/check.sh` was not run for the two `Propose:` commits
  (it needs the prod router); their statements are an unnest insert, a
  plain delete and a grouped select, the shapes UpsertSpotPrices already
  routes.
- Cap: at 21:44 UTC two runs whose pinned zone refused m7i.large failed
  at once and left their 2 instances each running, 4 at once, for under a
  minute until the sweep. The test now runs each kind's cycles in turn and
  launches in the next zone instead.

## Cleanup

Every run ended with the test's sweep and /tmp's shell sweep (cancel open,
active or disabled acceptance Spot requests, terminate pending, running,
stopping, stopped or shutting-down acceptance instances) in us-east-2,
us-west-1, us-east-1 and us-west-2. Final sweep at 22:03 UTC: no live
instance, no open request and no volume tagged `lazycloud:fleet =
acceptance`; the six acceptance Spot requests are `cancelled`. Nothing
tagged `lazycloud-prod` was touched; the prod reads were the fleet subnets'
zone ids and the deployed role's policy.

## Verification

- Real EC2 in `AWS_PROFILE=default` (534742592531, the platform account),
  after `aws sts get-caller-identity --profile default`; everything tagged
  `lazycloud:fleet = acceptance`, at most 3 instances at once, under an
  hour, swept at the end of every run. Never the `lazycloud-prod` tag.
- `iam simulate-principal-policy` against the deployed scheduler role, with
  this branch's fleet statements added.
- Prod steps wait for the integrator's word that the Ship is done; they use
  the dashboard and SDK as the user does and delete the apps they deploy.

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
