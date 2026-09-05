# Provider provisioning

One scheduler and compute service manage provider capacity. A cloud adapter
translates that service's requests into cloud API calls. Providers do not add
their own scheduler loops, worker agent, billing flow, or application release.

## Ownership

Terraform owns persistent infrastructure and image identities. Helm owns capacity
policy, prices, and secret property bindings. Packer owns prepared host images.
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

AWS infrastructure comes from Terraform and fleet policy comes from Helm.
`fleet-ensure` registers and validates the cloud account through the API.
Hetzner follows the same path. The values renderer combines its Terraform image
catalog with Helm policy, and composition resolves the provider registry.
It needs no separate
capacity-configuration command or manually copied workspace UUID. The bootstrap
workspace reference resolves only when capacity is used, after administrator
bootstrap has created it. Keep that configured workspace identity stable.

The default deployment requires a verified Ashburn image catalog and a
`hetzner:platform` token. Missing images fail Terraform validation; missing
configured provider tokens fail application settings validation. Supplying
credentials is still an operator prerequisite, just as it is for other services.

For an existing deployment, add the token-map field to its operator secret
before applying the new secret mapping. Preserve all other fields and check for
concurrent operator changes before publication. Ship reads the non-secret
infrastructure descriptor published by Terraform;
it does not apply infrastructure changes for you.

For the first Hetzner activation, explicitly advance the host release pin to a
release containing provider-token enrollment. Routine Ship retains the existing
host pin. Reusing a pre-Hetzner agent would leave a valid image unable to enroll.

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

Adapters return the same typed snapshots, machine identities, billing clocks,
and provisioning phases. They must not report a missing, never-created unit
as a successfully deleted unit. Provider uncertainty remains observable and
retryable; retries must not create duplicate billable resources.

## Necessary provider differences

AWS uses IAM roles, its VPC/subnets, AMIs, and Auto Scaling groups. Hetzner uses a
project token, snapshots, and labeled server groups. AWS can verify signed
instance identity; Hetzner uses launch-scoped single-use enrollment credentials.
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

Host-image preparation has passed. Full live workload acceptance is outstanding.
Check CI for the exact release commit. Local owner checks do not establish either.

For teardown, stop new workload admission, lower warm floors, and drain through
the compute service before removing credentials or persistent infrastructure.
Preserve the binding and current application until exact cleanup is proven.
Deleting a warm node alone triggers its replacement and is not a teardown.
