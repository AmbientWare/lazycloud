# Multi-provider placement and pricing plan

## Implementation status

The combined feature branch remains a draft. Provider-neutral capacity, warm
and cold lifecycle, GPU CPU backfill, region contracts, and forward billing
migrations are implemented. Regional prices are not published. Merge and deploy
are on hold until the owner finishes the other feature and releases the hold.

The simulation is checked local validation material. Its 192 main paths and
48 sensitivity paths reconcile monthly and annual accounting against the final
reserved-memory controller. Its code and generated outputs are excluded from
the pull request and shipped product. Demand remains estimated. These scenarios
do not justify lower compute prices or establish an overall margin improvement
from CPU backfill, so the backfill merge condition below remains unmet.

An additional 48 paired paths compare CPU-only, 5% GPU, 10% GPU, and the original
mix across 100, 500, and 1,000 accounts. Their 576 monthly records reconcile to
annual totals. The original mix assumed about 22% GPU jobs but incurred about
95% of host costs on GPU machines. CPU-heavy cases improve the modeled margin;
they are controlled scenarios, not measured customer demand.

Hetzner live acceptance and credential publication are outstanding. Terraform
owns the default Ashburn policy and image catalog, and the image workflow exports
verified non-secret Terraform input. Provider credentials remain separate.
The scheduler uses one pooled-capacity interface for AWS and Hetzner. Cleanup
uses recorded unit identity rather than depending on the current offer catalog.
The common operator and extension contract is in `deploy/PROVIDERS.md`. The owner
approved acceptance on production with Ashburn warm from startup, other pools
cold, and multiple node sizes. The merge/deploy hold above still applies.
Source IP and metadata cannot authenticate a host against its own tenants.
A node-scoped, short-lived, single-use bootstrap token has owner approval for
delivery in that node's user-data. The narrow exception is recorded in
`packages/compute/AGENTS.md`. Durable launch authorization consumes the token
and binds a node-generated credential. Retries resume the existing enrollment.
The forward `0007` migration preserves deployed state. Host credential
publication is atomic and root-only; API validation does not echo credentials.
A host image was baked in Ashburn, `ash`, using temporary Hetzner servers.
Snapshot `428154317` is retained. The exact build servers, primary IPs, and
temporary SSH keys were verified absent; the preexisting project SSH key remains.
This proves image preparation, not secure enrollment or a customer workload.

The selected project's token is stored outside the repository with owner-only
permissions. Deployment still requires the protected operator configuration and
the disposable workload acceptance in `deploy/hetzner/README.md`.
The documented `default-test` AWS profile currently
receives AccessDenied when assuming its operator role. Root credentials are not
an alternative for acceptance or deployment automation.

## Outcome

LazyCloud will sell two plans, Free and Team, with usage-based compute,
storage, and egress.

- Automatic placement is the default and the base compute price.
- Hetzner is the expected default CPU supplier because its offers should win
  on cost. The scheduler still chooses from live offers rather than checking a
  provider name.
- The default platform region keeps a small adaptive warm CPU pool.
- Every other region starts at zero machines and provisions from a prepared
  image when work arrives.
- GPU pools start at zero unless measured demand later pays for a warm floor.
- Free uses automatic placement. Team may choose a supported region.
- A selected region changes the compute rate class. Customers never select a
  supplier or set a rate class themselves.
- Account admission owns plan concurrency and billing limits. No new per-provider
  purchase ceilings or Hetzner node caps are required.

The initial pricing target is a 40% direct gross margin after supplier compute,
provider disks and network charges, payment fees, launch failures, idle
capacity, and billing-rounding waste. The simulator must set the actual prices
and regional multipliers. No placeholder multiplier reaches the public rate
card.

## Decisions that should not drift during implementation

### Public placement contract

Add `region: str | None` to the canonical workload resources. Omitted or
`null` means Automatic placement. Region IDs are LazyCloud product regions,
not AWS, Hetzner, or another supplier's location codes.

Reject a request that specifies both `region` and an explicit machine `pool`.
A pool already pins capacity, so accepting both would leave two authorities for
placement.

The pricing catalog publishes:

- Automatic placement and its explicit CPU, memory, and GPU rates.
- Each supported product region, its display name, availability, rate class,
  customer-facing multiplier, and explicit resource rates.
- The current global storage and egress rates.

The customer sees a multiplier for comparison. Metering reads the explicit
rates for the chosen class and never performs floating-point multiplication.

### Placement and billing identity

Use the existing `provider_ref` as the stable provider binding. Do not add a
second provider-binding ID. `provider_connection_id` remains an optional
foreign key for customer-connected AWS capacity. Platform Hetzner and other
platform suppliers do not need an AWS connection row.

The provider resolver returns the facts generic compute needs: provider ref,
capacity workspace, pool, platform-fleet status, allowed regions and instance
types, existing AWS connection limits, root disk policy, idle policy, and provider
implementation. Generic compute must not read `AwsAccountConnection`.

At dispatch, placement resolves a private `rate_class` from the accepted
region and selected offer. The container billing shape records that class once.
A repeated reservation or retry that proposes a different class fails.
Published effective-date rate changes may price future seconds differently,
but completed ledger segments retain their exact rate and pricing version.

Egress remains a separate global customer rate. Supplier charges for image
pulls, model transfer, object storage access, and cross-provider traffic belong
in the regional loaded-cost model. They do not silently become customer egress.

### Warm and cold capacity

Cold means zero machines, not powered-off machines. The provider-specific image
and bootstrap artifact remain ready. The first request creates a server, and
other compatible requests coalesce onto that pending capacity.

The default Hetzner CPU region starts with one warm node. The controller later
sets its target from recent compatible arrivals during the measured p95 launch
and enrollment interval. It raises the target promptly and lowers it one node
at a time after sustained low demand.

All other regions and all GPU pools start with a floor of zero. An idle machine
is deleted only when it holds no work or reservation. Providers with a billing
quantum keep it until near the end of the already-paid interval, then delete it
before the next interval starts. This avoids paying for five minutes and
discarding the remaining paid hour.

### Offer selection and failure behavior

The existing offer filter and cheapest-compatible selection remain the base.
There is no supplier-price ceiling. If no offer meets the workload, region, and
reliability requirements, the request stays queued or fails with a named
capacity error. A more expensive compatible offer may be selected, so placement
does not guarantee a profitable margin. Pricing review must account for this.

### GPU CPU backfill

Keep ordinary CPU and GPU placement separate. Add a second, explicit backfill
path only for CPU requests already marked preemptible.

Backfill is allowed after normal FIFO CPU placement fails, when a compatible
GPU machine already exists, its GPUs are occupied, no compatible GPU request or
capacity reservation is waiting, and the CPU request fits the remaining CPU
and memory.

Persist that the container is backfill. When GPU capacity becomes available
and compatible GPU work cannot fit, atomically stop new backfill, evict only
marked backfill containers, requeue them with `Preempted`, wait for resource
release, and dispatch the GPU request. Never evict a GPU workload or an ordinary
CPU workload. Do not launch a GPU machine for CPU work.

## Parallel execution map

The implementation uses one feature branch and one combined pull request, as
requested by the owner. Agents edit disjoint owners in the shared worktree.
The manager reviews each returned change and runs integrated acceptance. Root
workspace files, dependency manifests, package exports, and lockfiles belong to
the integration owner unless a slice names them explicitly.

### Wave 1: foundations and simulation contracts

These three agents can start together because their files do not overlap.

#### Agent 1A: provider-neutral capacity foundation

Own:

- `packages/shared/src/shared/compute_policy.py`
- `packages/compute/src/compute/providers.py`
- `packages/compute/src/compute/service.py`
- `packages/compute/src/compute/request_placement.py`
- `packages/compute/src/compute/provider_machines.py`
- `packages/database/src/database/tables/compute.py`
- `packages/database/src/database/repositories/compute.py`
- New `packages/database/src/database/alembic/versions/0005_provider_neutral_capacity.py`
- `packages/compute/AGENTS.md`

Work:

- Make `provider_ref` the durable provider-neutral binding.
- Make the AWS connection pointer optional for platform capacity.
- Move pool, ownership, limits, allowed offers, disk, and idle policy behind the
  resolved provider binding.
- Remove AWS repository reads and AWS-only error text from generic placement,
  acquisition, scaling, reconciliation, and cleanup.
- Update the compute architecture decision that currently says provider breadth
  is not planned.
- Update SQLAlchemy metadata and add the forward `0005` Alembic revision chained
  to `0004_machine_draining`. Preserve every existing compute unit and AWS
  connection while relaxing the AWS-only relationship for platform capacity.

Proof:

- Existing connected-AWS capacity still provisions through the neutral path.
- A platform pooled provider resolves without an AWS connection row.
- Restarted reconciliation resolves the same provider through `provider_ref`.
- Upgrade a PostgreSQL database at `0004` with existing compute rows and prove
  they retain the same ownership and provider identity.
- A clean PostgreSQL bootstrap also enforces the revised internal-unit
  invariants.

#### Agent 1B: region and rate-class contract foundation

Own:

- New `packages/shared/src/shared/placement.py`
- `packages/shared/src/shared/workload_config.py`
- `packages/shared/src/shared/deployment_records.py`
- `packages/shared/src/shared/http/stubs.py`
- `packages/shared/src/shared/http/deployments.py`
- `packages/shared/src/shared/http/gateway.py`
- `packages/shared/src/shared/scheduling.py`

Work:

- Define normalized product-region IDs and opaque placement rate classes.
- Carry optional region selection through every workload kind and scheduler
  request while preserving omitted versus explicit values.
- Carry the resolved rate class on the authoritative placement result.
- Reject `region` with an explicit pool at the public validation boundary.
- Define signatures only. Later agents consume them rather than editing them.

Proof:

- Function, endpoint, ASGI, pod, and sandbox contracts round-trip omitted and
  selected regions without filling an omitted value.
- Conflicting region and pool input fails before persistence or provisioning.

#### Agent 1C: provider assumptions and simulation schema

Own:

- New `work/pricing-provider-assumptions.json`
- New `work/pricing-provider-sources.md`
- New `work/pricing_simulation/contracts.py`

Work:

- Record dated supplier offers and sources for Hetzner, current AWS, and GPU
  candidates.
- For each offer capture region, resources, node and disk cost, billing minimum
  and quantum, capacity, launch and registration latency distributions, image
  cache latency, launch failures, interruptions, public egress, cross-provider
  traffic, and data-location constraints.
- Define the versioned input and output JSON schemas used by Python and the
  optional HTML viewer.
- Keep uncertain values marked as estimates with a range. Do not turn a
  competitor's fixture or promotional price into a production assumption.

Proof:

- Schema validation rejects incomplete offers and unnormalized regions.
- Every production assumption has a source date or is labeled as an estimate.

### Wave 2: supplier, billing, and event simulation

Start after Wave 1 is integrated on the feature branch. These agents own
disjoint packages.

#### Agent 2A: Hetzner pooled provider

Own only:

- New `packages/providers/hetzner/**`

Work:

- Implement offer discovery, create, describe, capacity changes, individual
  release, unit deletion, and authoritative proof that server, attached storage,
  and allocated IP are gone.
- Use the common bootstrap builder with a Hetzner identity fragment.
- Label every server with the unit, generation, offer, and platform ownership.
- Implement idempotency so a control-plane restart cannot double-create a
  server.
- Prove the enrollment identity design against a live disposable server. Do not
  embed a reusable authenticator in cloud-init. Provider API labels, expected
  address, and metadata are not host authentication because tenant code shares
  the host's public source IP. Establish an independently authenticated host
  identity before enrollment succeeds. The credential delivery decision above
  must be resolved before implementing that boundary.

Proof:

- A live disposable Hetzner server creates, enrolls, runs a real container,
  drains, deletes, and leaves no server, disk, or IP behind.
- Replaying create and enrollment does not create or admit a second machine.

#### Agent 2B: billing rate classes and plan admission

Own:

- `packages/shared/src/shared/billing_quotes.py`
- `packages/shared/src/shared/billing_rate_card.py`
- `packages/shared/src/shared/billing_plans.py`
- `packages/shared/src/shared/http/pricing.py`
- `packages/database/src/database/tables/billing_rates.py`
- `packages/database/src/database/tables/billing_ledger.py`
- `packages/database/src/database/repositories/billing_rates.py`
- `packages/database/src/database/repositories/billing_ledger.py`
- New `packages/database/src/database/alembic/versions/0006_placement_rate_classes.py`
- `packages/billing/src/billing/admission.py`
- `packages/billing/src/billing/costs.py`
- `apps/cli/src/cli/billing.py`

Work:

- Add rate class to compute-rate identity, container billing shape, and compute
  ledger segments.
- Update rate uniqueness and effective-window exclusion constraints to include
  billing owner, rate class, and GPU type.
- Make duplicate billing-shape recording reject a changed class.
- Add a `region_selection` entitlement. Free rejects a selected region before
  capacity work. Team accepts supported regions.
- Keep egress global and independent of the compute class.
- Publish no regional rate until the simulation supplies an approved value.

Proof:

- Upgrade a PostgreSQL database at `0005` with existing rates, billing shapes,
  and ledger rows. Every old row becomes the Automatic class without changing
  its charge.
- A clean PostgreSQL bootstrap creates the intended constraints.
- Identical Auto and selected-region containers produce their exact published
  compute charges.
- A retry cannot mutate a container's class.
- A later effective rate changes only future ledger segments.
- Free fails before provisioning and Team reaches placement.

#### Agent 2C: discrete-event Monte Carlo engine

Own:

- New `work/provider_pricing_simulator.py`
- New modules under `work/pricing_simulation/`, except `contracts.py`
- Generated Markdown, CSV, and JSON outputs under `work/`

Do not edit either HTML file.

Work:

- Replace sampled one-hour packing ratios with a discrete-event machine model.
- Run 12-month paths at exactly 100, 500, and 1,000 active card-on-file
  accounts. Hold account count fixed. Show cardless trials and their cost
  separately.
- Make Free and Team shares explicit assumptions. Do not hide them behind a
  steady-state conversion formula.
- Generate jobs with arrival time, burst or session, resources, runtime,
  customer, plan, placement choice, transfers, and preemptibility.
- Track machines through requested, starting, ready, busy, idle, and deleted.
- Pack online with the production best-fit rule, coalesce onto pending machines,
  apply launch failures and retries, round provider lifetimes, and record queued
  or rejected work.
- Compare zero, one, and adaptive warm nodes in the default Hetzner region.
  Keep every other region and all GPUs cold initially.
- Compare current placement with safe GPU CPU backfill.
- Separate operational Monte Carlo variation from sensitivity analysis of Team
  share, usage, runtime, supplier price, regional share, and boot time.
- Derive usage charges from resource-seconds. Never sample a monthly dollar
  total and convert it back into jobs.

Proof:

- Seeded results reproduce exactly.
- Resource use never exceeds machine capacity.
- Every supplier charge reconciles to machine lifetimes and billing quanta.
- Every customer charge reconciles to resource-seconds, storage, and egress.
- Subscription credits apply once per account and period.
- Percentile estimates stabilize across successive trial batches.

### Wave 3: resolver, capacity policy, and public authoring

#### Agent 3A: aggregate provider resolver

Own only:

- `packages/provider-clients/**`

Work:

- Replace the AWS-only compute resolver with an aggregate resolver.
- Return configured platform Hetzner capacity, configured platform fallback
  suppliers, and customer-connected AWS capacity.
- Define one platform capacity workspace and stable provider refs.
- Keep credentials in deployment secrets.
- Let offer cost and policy select Hetzner. Do not special-case its name.

Proof:

- Resolver lists all configured bindings with distinct refs.
- Missing or invalid provider configuration fails by provider name.
- A workspace without connected AWS still sees platform capacity.

#### Agent 3B: warm, cold, and billing-aware lifecycle

Own:

- `packages/compute/src/compute/offers.py`
- `packages/compute/src/compute/provider_machines.py`
- `packages/scheduler/src/scheduler/pool_drain.py`
- `packages/scheduler/src/scheduler/pool_sizing.py`

Work:

- Add provider-neutral billing minimum and quantum to offers and durable
  provider instances.
- Preserve and use the existing `billing_renewal_at` instead of clearing it on
  reconciliation.
- Implement billing-boundary-aware idle deletion.
- Add the adaptive platform warm controller for the default Hetzner CPU region.
- Keep non-default regions and GPU pools at zero.

Proof:

- The default CPU region maintains its calculated floor.
- Other regions begin and return to zero.
- Idle deletion stays inside the paid quantum without crossing the next one.
- An unavailable cheap offer selects the next compatible offer.

#### Agent 3C: SDK and CLI region authoring

Own only:

- `packages/lazycloud/**`

Work:

- Add optional `region` to Function, Endpoint, ASGI, Pod, and Sandbox authoring.
- Carry it through deployment overrides and spec-to-gateway conversion.
- Add clearly labeled `--region` inputs where the CLI authors those workloads.
- Preserve omission as Automatic placement.

Proof:

- One public SDK and CLI workflow deploys with Automatic placement and with a
  supported selected region.

### Wave 4: integration, web, and GPU backfill

#### Agent 4A: process and provider-enrollment integration

Own:

- `apps/api/**`
- `apps/scheduler/**`, except scheduler files assigned to Agent 4C
- `apps/agent/**`
- `packages/gateway/**`
- `packages/shared/src/shared/http/provider_nodes.py`
- `deploy/**`
- Operator documentation
- Root workspace manifests, package exports, and lockfiles

Work:

- Compose the aggregate resolver in API and scheduler processes.
- Replace AWS-only node proof routing with a provider verifier registry.
- Add Hetzner secrets, release image inputs, and scoped teardown instructions.
- Map product regions to provider regions in deployment configuration.
- Keep every provider failure explicit. Do not add fallback imports or fake
  local success paths.

Proof:

- Existing connected-AWS workflows still use the common lifecycle.
- Hetzner enrollment refuses a mismatched server identity or generation.
- Provider cleanup is scoped to resources created by the acceptance run.

#### Agent 4B: web pricing and region input

Own only:

- `apps/web/**`

Work:

- Mirror the pricing, entitlement, stub, and deployment contracts in Zod.
- Add a labeled region selector to workload configuration. Explain Automatic
  placement next to the field.
- Show the chosen region and multiplier before deployment.
- Show only Free and Team on pricing and plan surfaces.
- Read all plans, rates, labels, and region options from `/api/v1/pricing`.
- Keep the Python JSON report as simulation truth. The HTML simulator is a thin
  viewer and performs no business calculations.

Proof:

- Bun typecheck and build pass.
- A browser run shows understandable labels, Automatic placement, a supported
  Team region, and the exact price difference returned by the API.

#### Agent 4C: safe GPU CPU backfill

Own:

- `packages/scheduler/src/scheduler/tools.py`
- `packages/scheduler/src/scheduler/containers.py`
- `packages/scheduler/src/scheduler/preemption.py`
- `packages/scheduler/src/scheduler/state.py`
- The smallest focused scheduler tests needed for the concurrency boundary

Work:

- Add the explicit backfill classification and eligibility checks.
- Add selective, fenced eviction and requeue of marked backfill containers.
- Prevent duplicate eviction and GPU starvation under competing schedulers.
- Preserve account, workspace, pool, and worker ownership checks.

Proof:

- Real Redis evidence shows only marked backfill is evicted.
- Concurrent eviction is idempotent.
- A waiting GPU request eventually acquires its reserved CPU, memory, and GPU.
- No GPU or ordinary CPU container is stopped by backfill recovery.

### Wave 5: integrated acceptance and rate decision

The manager owns this wave. Do not merge pricing or enable a supplier from a
subagent's isolated result.

Run:

- Seeded 12-month simulations at 100, 500, and 1,000 active accounts.
- A live default-region Hetzner warm-pool workflow.
- A live cold-region create, enroll, run, reuse, and delete workflow.
- Failover to the next compatible offer with cost attribution preserved.
- Existing connected-AWS provisioning, enrollment, scaling, and teardown.
- Automatic and Team region-selected billing through API, ledger, invoice
  outbox, SDK, CLI, and web display.
- Cleanup proof for every external server, volume, IP, image, and temporary
  credential created by acceptance.

Only then select:

- Base CPU, memory, and GPU rates.
- Supported GPU supplier and catalog.
- Regional rate classes and multipliers.
- Default-region warm policy and billing-aware idle interval.

## Simulation outputs

Every metric reports mean, p10, p50, and p90. Money appears per month and per
year. The annual result sums the twelve simulated months. A monthly run rate
multiplied by twelve may also appear, but it must be labeled separately.

Business outputs:

- Team subscription MRR, usage revenue, credits, collected revenue, and ARPA.
- Provider compute, disk, storage, network, payment, and platform costs.
- Gross profit, gross margin, operating profit, company reserve, tax reserve,
  and estimated owner take-home.
- Provider and region revenue, cost, and margin.

Operational outputs:

- CPU, memory, and GPU utilization.
- Paid node-hours, customer resource-hours, idle cost, and billing-rounding
  waste.
- Jobs served, queued, failed, and retried.
- Startup latency p50, p95, and p99.
- Warm hits, cold starts, launches, and deletions.
- Backfill revenue, evictions, retries, and lost work.

The report defines the money terms beside the numbers:

- Gross profit is collected revenue less variable provider and payment costs.
- Gross margin is gross profit divided by collected revenue.
- Operating profit is gross profit less fixed platform, support, and replacement
  acquisition costs.
- Estimated owner take-home is positive operating profit less the company and
  tax reserves. It is a planning figure, not payroll or tax advice.

## Merge rules

- Merge both Wave 1 foundations before rebasing dependent branches.
- Merge `0005_provider_neutral_capacity` before the billing branch creates and
  tests `0006_placement_rate_classes`. Never create competing Alembic heads.
- An agent edits only its assigned files. The manager resolves shared exports,
  manifests, dependency locks, and composition.
- Do not combine the provider adapter with public pricing. A live supplier can
  exist while its regional class remains unpublished.
- Do not enable CPU backfill until the event simulation shows it improves total
  margin without increasing GPU starvation.
- Do not advertise a region until image availability, identity verification,
  cleanup, and published pricing have live evidence.
- Any unavailable external credential is an acceptance gap. Do not replace it
  with a mock provider.
