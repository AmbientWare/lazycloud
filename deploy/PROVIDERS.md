# Provider provisioning

Platform purchases require a 30% margin at full sellable CPU, RAM and GPU
capacity. Compute subtracts scheduler headroom and includes supplier compute,
root disk and public IPv4 costs. Missing quotes and offers below the floor are
excluded before purchase. Non-preemptible work uses the published CPU/RAM
premium; Spot-tolerant work retains its lower rate on On-Demand capacity.
The floor does not guarantee utilization or cap later Spot price changes.

One scheduler and compute service manage provider capacity. A cloud adapter
translates that service's requests into cloud API calls. Providers do not add
their own scheduler loops, worker agent, billing flow, or application release.

## Ownership

Terraform owns persistent platform infrastructure. Deployment image builders
publish host images, and deployment configuration records their verified identities.
Provider code owns capacity policy and supplier price assumptions. Providers
with price APIs supply current quotes. Helm owns secret property bindings.
The normal deployment supplies
configuration and credentials to the application. The compute service owns
durable units, desired capacity, mutation fencing, retries, and drain decisions.
Adapters own cloud resource operations and provider identity verification.

Do not declare dynamically scaled worker servers in Terraform. Terraform and
the scheduler must not compete to set the same fleet's size. Customer-owned
AWS onboarding remains a separate account-authorization workflow; once resolved,
its capacity implements the same provider interface as platform capacity.

## One operator workflow

1. Prepare provider credentials and host images. Keep credentials in the
   deployment's operator secret, not Terraform inputs or image artifacts.
2. Review the deployment's Terraform plan with its image catalog. Apply the
   plan using the existing state and approved operator identity.
3. Run Ship from main after the PR checks pass and the owner approves release.
   It publishes the common application artifacts and records the deployment.
   Argo runs migrations and administrator bootstrap before application workloads.
   Fleet registration runs after the API is ready.
4. Verify the real workload path, billing, scaling, and cleanup. An image build
   or healthy control plane alone is not provider acceptance.

AWS infrastructure comes from Terraform. Typed provider definitions live in
their provider packages and are registered in
`provider_clients/provider_definitions.py`. Each definition owns its approved
locations, instance catalog and `policy.purchases_enabled` switch. Change these
through a reviewed PR. Customers select workload resources; LazyCloud manages
node selection and lifecycle.

Disabling purchases stops new capacity and replacements. Existing nodes remain
observable and drain through the compute service. AWS reconciliation also
suspends Auto Scaling launches so the provider cannot replace a node independently.
The connection role needs `autoscaling:SuspendProcesses` and
`autoscaling:ResumeProcesses` on its owned groups before changing AWS purchase
permission. Keep management credentials until existing units have completed cleanup.

Migration `0017_managed_compute_policy` requires stopping the old control-plane
and scheduler processes before it runs. Old processes can restore the removed
settings when saving a connection. Pause Argo automatic sync while building the
release, stop those processes, then sync the new deployment. Restore automatic
sync after the migration and new application processes are healthy.

`fleet-ensure` registers and validates the cloud account through the API.
The values renderer supplies Hetzner's Terraform image catalog as
`LAZYCLOUD_PLATFORM_CAPACITY_HETZNER_IMAGES`. When images are present, it binds
`LAZYCLOUD_PLATFORM_CAPACITY_HETZNER_TOKENS` from the operator secret to the API
and scheduler. Helm renders these through its normal environment and secret
bindings; it contains no provider registry. Application composition reads the
Python registry and resolves the bootstrap workspace when capacity is used.

AWS provider code defines instance rates and regional gp3 and public IPv4 prices.
Each node quote includes its requested gp3 disk size and one public IPv4 address.
Disk estimates
use a 30-day month, matching the normalization in [AWS's EBS pricing examples](https://aws.amazon.com/ebs/pricing/),
and round upward to whole hourly USD micros. The production us-east-1 inputs
are $0.08/GiB-month and [$0.005/public-IP-hour](https://aws.amazon.com/vpc/pricing/),
verified on 2026-09-09. Add reviewed supplier cost data before enabling another region.

Enabled Hetzner purchases require verified images for approved locations and a
`hetzner:platform` token. Missing images fail deployment validation; missing
configured provider tokens fail application settings validation. A fresh
deployment with Hetzner purchases disabled may omit both. Disabling purchases
on an existing deployment retains its images and credentials for cleanup.

For an existing deployment, add the token-map field to its operator secret
before applying the new secret mapping. Preserve all other fields and check for
concurrent operator changes before publication. Ship reads the non-secret
infrastructure descriptor published by Terraform;
it does not apply infrastructure changes for you.

Hetzner uses the selected release's agent for `provider-bootstrap` enrollment.
Ship selects that agent along with the worker and platform images. Host replacement
uses the existing surge and drain controller.

## Scheduler and adapter boundary

The scheduler calls capacity acquisition and release on the compute service.
It does not inspect a cloud name to choose a method. The service resolves a
`provider_ref` through `ComputeProviderResolver` and calls
`PooledCapacityProvider`:

| Method | Contract |
| --- | --- |
| `list_offers` | Currently purchasable shapes, resources, locations, and costs. |
| `unit_offer` | An owned unit's recorded shape, independent of the sale catalog. |
| `ensure_unit` | Reconcile the requested unit and desired capacity idempotently. |
| `describe_unit` | Observe owned resources without buying replacements. |
| `set_unit_capacity` | Apply capacity intent under the service's mutation lease. |
| `release_machine` | Release the exact machine selected and fenced by drain. |
| `delete_unit` | Remove the unit's owned resources; report deletion progress. |
| `machine_storage_destroyed` | Prove exact resource absence before retiring data ownership. |

The purchase catalog and owned resources have different lifetimes. New
acquisition requires an available compatible offer. Observation and cleanup
must still work if that offer disappears or the catalog endpoint fails. A
provider binding, its credentials, and necessary image metadata must remain
available until its units have completed cleanup.

Zero-capacity reconciliation observes the owned unit and applies zero without
consulting the purchase catalog or creating a missing unit. Idle retirement
does not depend on a replacement enrolling. The shared drain service preserves
warm floors and provisioning reservations; compute also checks durable live
work across every workspace using the unit before scaling it to zero.

Adapters return the same typed snapshots, machine identities, billing clocks,
and provisioning phases. They must not report a missing, never-created unit
as a successfully deleted unit. Provider uncertainty remains observable and
retryable; retries must not create duplicate billable resources.

## Necessary provider differences

AWS uses IAM roles, its VPC/subnets, AMIs, and Auto Scaling groups. Hetzner uses a
project token, snapshots, and labeled server groups. AWS can verify signed
instance identity; Hetzner uses launch-scoped single-use
enrollment credentials verified against provider inventory and durable launches.
These differences stay in adapters and composition. All enrolled nodes use the
same agent, runtime, WireGuard control path, worker protocol, and metering.

Details: [AWS infrastructure](platform-deployment/README.md),
[AWS images](ami/README.md), [Hetzner images and credentials](hetzner/README.md).

## Adding a provider

Use an explicit package under `packages/providers/<provider>`, registered by
`packages/provider-clients`. This is a compiled-in adapter model, not arbitrary
runtime plugin loading. Unsupported providers must fail explicitly.

Implement the interface above, typed configuration and identity verification,
and any persistent infrastructure and image recipe the cloud needs. Keep
provider credentials separate from deployment settings. Add the adapter to
composition without changing scheduler algorithms or duplicating agent logic.
Do not add placeholder GCP or Azure implementations before their real APIs and
identity contracts are implemented and verified.

Acceptance uses the real provider to create, enroll, run, reuse, bill, drain,
and delete capacity. Cover uncertain creates, enrollment replay, restarts,
expired credentials, and exact cleanup. GCP and Azure are not supported today.

## Current rollout and verification

The owner approved merge and deployment after release checks, with one adaptive
Ashburn warm CPU baseline and all other platform pools cold from startup.

After rollout, submit small and larger CPU/memory jobs to prove reuse and
resource-based node selection, then an AWS GPU job. Poll durable units,
provider resources, enrollment, scheduler state, and worker logs together.
Verify results and usage charges. Confirm cold nodes and their IPs disappear
after drain, while the intended warm baseline remains. Do not reset production
data or delete unrelated resources during acceptance.

Use a billing-enabled account for workload checks. An administrator token does
not bypass billing admission. A billing refusal must finish the image build as
failed with its reason and clean up dispatch credentials, not keep retrying.

Host-image preparation has passed. Full live workload acceptance is outstanding.
Check CI for the exact release commit. Local owner checks do not establish either.

For teardown, stop new workload admission, lower warm floors, and drain through
the compute service before removing credentials or persistent infrastructure.
Preserve the binding and current application until exact cleanup is proven.
Deleting a warm node alone triggers its replacement and is not a teardown.
