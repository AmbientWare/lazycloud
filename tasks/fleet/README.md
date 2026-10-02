# Fleet capacity packets

The design is tasks/fleet/plan.md; the rules are tasks/fleet/parity.md. These
files live on `fleet-capacity-plan`, which is also the integration branch.
They are removed before the final merge to main.

## Decisions

The user decided on 2026-10-02:

- Reserve floors: keep the reference's numbers. They can change later.
- Proposals P1 (prove hibernation from the agent's resume report) and P2
  (read EC2 quotas before buying) are approved; the packets that own them
  deliver them, each in its own commit. P3 and P4 are not approved.
- The end-to-end resume check runs on prod after the Ship.

## Packets

| Packet | Branch | Migration | Shared-file range | Depends on |
| --- | --- | --- | --- | --- |
| [policy](policy.md) | `fleet-policy` | none | new `internal/compute/fleet_*.go` | none |
| [provider](provider.md) | `fleet-provider` | 0003 | `cmd/scheduler/main.go`: Spot price loop and actuator call | none |
| [agent-resume](agent-resume.md) | `fleet-agent-resume` | 0004 | host.proto `Hello` 72-73, `ServerMessage` 71, `HostMessage` 71 | provider's schema commits |
| [planner](planner.md) | `fleet-planner` | 0005 | `cmd/scheduler/main.go`: pass cadence | policy, provider, agent-resume |
| [api-web](api-web.md) | `fleet-api-web` | none | OpenAPI descriptions of `FleetState`, `FleetMarket` | planner |
| [acceptance](acceptance.md) | `fleet-acceptance` | none | none | all |

## Waves

1. Wave 1, in parallel: policy, provider, agent-resume. The provider packet
   pushes its reconcile move and schema first; agent-resume branches from
   that commit.
2. Wave 2: planner, once wave 1 has merged.
3. Wave 3: api-web (it may start against plan.md's schema during wave 2),
   then acceptance.
4. Final: one integration review of `fleet-capacity-plan` against main,
   remove tasks/fleet, one PR to main, one Ship to `lazycloud-prod` with the
   user's go-ahead, then the acceptance packet's prod steps.

Merge into `fleet-capacity-plan` in this order so migration numbers match the
chain: provider (0003), agent-resume (0004), policy, planner (0005),
api-web, acceptance. Tell running agents when the base moves.

## Merge gate

Each packet goes through the same gate before it merges into
`fleet-capacity-plan`:

1. `./check.sh` and focused `go test -race` on the touched packages pass with
   visible output; generated code is current (`go generate ./...`, `bun run
   apigen`, datamodel-codegen profiles) and the tree is clean after.
2. Every new or changed statement is Neki-safe: no ARRAY(subquery), writable
   CTEs, subqueries in UPDATE SET or RETURNING, correlated LIMIT, subqueries
   inside ORDER BY expressions, window functions over joins, or
   DELETE ... USING. A migration does not read or write a table it creates.
   Nothing bypasses the router.
3. A separate read-only reviewer gets the diff and this focus list: phase
   transitions and lock order; idempotency of each EC2 call on retry and lost
   answers; goroutine and lease lifetimes; queries that scan history or the
   backlog; IAM scope (tag conditions on every mutating EC2 action); Spot
   request cleanup; cost (anything that could buy or keep hosts without a
   bound); dead code and contract drift. Only verified defects with a file,
   line, failure scenario and fix.
4. Fix every real finding with a test that fails without the fix.
5. Real EC2 checks ran in `default-test` as the packet file says, and every
   resource they created is gone.
6. Green means green: all checks passed, none pending. Squash with a
   one-line subject and a one- or two-line description.

The final PR to main also gets the final gate: a full code review, dead and
repeated code removed, prose condensed and unslopped.

## How the packets fit

- plan.md's intent table is the contract between the planner (writes
  intents), the provider actuator (performs EC2 calls) and the host session
  (agent proofs and resumes). A packet that needs to change it says so in its
  report; the integrator updates plan.md and tells the others.
- The policy packet's types are the planner's input and output. The planner
  proposes changes to them rather than editing them.
- Shared files: each packet edits only its migration number, its protobuf
  fields, its OpenAPI descriptions and the minimal wiring lines listed above.
  The integrator owns plan.md, this file, go.mod and root build files.
- Proposed differences P1-P4 in plan.md land only after the user approves
  them, each in its own commit.
