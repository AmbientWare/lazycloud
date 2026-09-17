# Platform deployment and account separation

Status: implemented on `feat/platform-deployment-separation`, pending release and
production acceptance. The ownership decisions below remain the implementation
contract. Follow [the cutover procedure](deploy/platform-deployment/PLATFORM_OWNERSHIP.md)
for deployment.

## Outcome

A LazyCloud deployment can initialize, restart, provision workers and recover
capacity without a human account or human API token. Platform compute belongs to
the deployment. Customer cloud connections remain account-owned.

Revoking Connor's credentials, disabling an administrator, or deleting a customer
workspace must not stop the platform fleet. Platform AWS must disappear from the
account's Compute settings because it is no longer a customer connection.

## Current deployment

Verified on September 17, 2026 UTC. Production is running 0.1.4 from
`68ac8b35000583baf2fc155b4a72efd8f7c37081`.
Argo reports Healthy, Synced and Succeeded. PostgreSQL reached
`0004_image_build_attempts`, and active release generation 39 is installed.

The deployment's `fleet-ensure` job failed because its account token was revoked.
The immediate repair minted `deployment-lazycloud-prod` for the existing
administrator and replaced only `LAZYCLOUD_TOKEN` in the existing operator secret.
The job then passed. This credential is still required by the current deployment;
remove it only after its consumers have been removed.

Two workers were observed accepting requests with the published 0.1.4 worker
digest and agent checksum. The published client built a fresh image, invoked
`square(7)`, streamed the function's log and returned `49`. AWS also issued
rebalance recommendations during rollout; the scheduler acquired replacement
capacity in another availability zone and recorded the recovery as complete.
This verifies the ordinary execution path and that observed recovery. Forced
interruption during an image upload remains unverified in production.

## Where the coupling lives

- `deploy/chart/templates/fleet.yaml` runs `lazycloud-admin fleet ensure` through
  the API with `LAZYCLOUD_TOKEN`. An application release therefore needs a valid
  human credential after the control plane starts.
- `apps/cli/src/cli/fleet.py`, the AWS resource router in `apps/api`, and
  `packages/compute/src/compute/aws_connections.py` pass the authenticated user's
  ID into `ensure_fleet`.
- `AwsAccountConnectionTable` requires `user_id`, uniquely limits connections per
  user, and cascades on user deletion. `platform_fleet` changes behavior without
  changing ownership.
- `AwsAccountConnectionDirectory.capacity_workspace` chooses the smallest UUID
  among the account's owned workspaces. `list_platform` loads all connections and
  filters them in Python.
- `WorkspaceComputeProviderResolver` mixes platform connections with customer
  connections. The configured platform provider path already used for Hetzner
  avoids the account connection, but still resolves a tenant workspace.
- `AuthService.create_service_token`, `validate_service_token`, and
  `apps/worker-bootstrap` require `administrator_ready`. Human initialization
  remains part of service credential lifecycle.
- `ProviderNodeIdentityVerifier` accepts `AwsAccountConnection` in a
  provider-neutral protocol. Removing the customer row without changing this
  contract would break node admission or weaken its checks.

## Ownership decisions

### Deployment configuration owns platform provider bindings

Extend `PlatformCapacitySettings` and `configured_platform_compute_providers` to
compose platform AWS directly. Terraform's infrastructure descriptor supplies
the account, control role, node role, instance profile, networks and stable
provider reference. Helm supplies application policy and secret bindings. The
release manifest continues to supply agent, worker and host artifacts.

Keep one canonical configuration. Do not copy the platform's role and networks
into a second account-connection record. PostgreSQL continues to own units,
instances, enrollment, purchases, recoveries and cleanup progress.

The AWS adapter should consume a validated AWS target shared by the platform and
customer resolvers. Rename `AwsConnectedAccountPooledProvider` if needed to reflect
that responsibility; remove the old name rather than retaining an alias.
Customer connect, authorization rotation, validation and disconnection remain
owned by `AwsAccountConnectionService`.

Keep the existing fleet's provider reference as its canonical opaque identity
during adoption. Changing ownership must not create duplicate pools, lose cloud
resource tags or reset pending purchases. New deployments receive a stable
reference from the same infrastructure owner. Stop inferring a customer
connection ID by parsing the reference.

### The platform has an internal namespace with no human owner

Use an explicit platform kind in the existing workspace namespace model. This
reuses the durable scope needed by compute records and service credentials;
it does not create a service user or grant anybody workspace ownership.

Create one protected platform namespace per deployment database, selected by
kind and a uniqueness constraint. Existing workspaces retain the tenant kind.
Do not convert the current `default` workspace, which contains customer data.
Do not recognize the platform namespace by a reserved name or metadata flag.

The platform namespace has no members, invitations, plan, customer storage, app
deployments or account API tokens. Public workspace selection, including an
administrator's selection, rejects it. Tenant list, billing, storage-retention
and deletion queries exclude it in SQL. Internal service entrypoints resolve it
through a dedicated identity service. Tenant deletion cannot select it.

Platform units and machine credentials belong to this namespace. User functions,
containers, images, volumes and usage retain their submitting workspace and
billing owner, even when a platform worker executes them.

### Deployment authority comes from the deployment

Add an idempotent platform initialization service in `packages/identity` and a
thin offline command in `apps/cli`. It creates the internal namespace and its
required key material using the deployment's database authority. It does not
create an administrator or publish a general-purpose bearer token.

The deployment job calls canonical domain services directly. AWS validation uses
the existing workload identity and configured assume-role boundary. CI keeps its
OIDC role; local platform operations keep AWS profile `default`. No local AWS
credentials enter workloads.

Keep the existing distinct machine, worker and gateway credentials and their
restricted permissions. Their issuance and validation depend on platform
initialization and the named resource's lifecycle, not `administrator_ready`.
Customer machine credentials retain customer scope. A platform worker credential
must not become an account credential or an unrestricted workspace credential.

First administrator enrollment and administrator recovery remain explicit human
operations. They may happen after the platform starts. Remove them from the
recurring deployment dependency graph.

## Implementation order

Implement this as one coherent cross-owner change, with the following internal
steps. Do not deploy a half-migrated ownership model.

1. **Separate initialization and namespace authorization.** Add the platform
   namespace kind, uniqueness and lifecycle restrictions in
   `packages/database`, the shared identity contracts and `packages/identity`.
   Introduce platform initialization and remove human readiness from service
   credential issuance and validation. Update worker bootstrap, public workspace
   authorization, membership, deletion, billing and storage selectors together.
   Keep customer defaults in human onboarding rather than platform startup.

2. **Resolve platform AWS from deployment configuration.** Extend
   `packages/provider-clients/src/provider_clients/settings.py` and
   `workspace_compute.py`, then API and scheduler composition in
   `apps/api/src/api/server/services.py`. Extend the versioned infrastructure
   descriptor in `deploy/platform-deployment/configuration.tf` and
   `deploy/chart_values.py` with the required node identity and stable reference.
   Read those identities from their Terraform owner, not guessed ARN defaults.
   Keep provider fields within the AWS binding. Remove `list_platform` and the
   platform use of `capacity_workspace` from the customer connection directory.

3. **Separate node authorization from customer connection records.** Replace the
   connection parameter in `packages/compute/src/compute/provider_nodes.py` with
   a precise resolved admission contract. The AWS implementation verifies the
   account, role, instance profile, region, pool membership and instance identity
   against the selected platform or customer binding. Update its consumers in
   enrollment and API composition. Preserve proof expiry, one-time enrollment,
   generation checks, revocation and tunnel certificate authority. Apply the
   internal namespace to `provider_launches.py` and its encrypted launch secrets
   as well, so configured platform providers share the same ownership model.

4. **Migrate existing platform state.** Append an Alembic revision after
   `0004_image_build_attempts`, or the newer head at implementation time. Adopt
   only explicitly identified `platform_fleet` resources. Verify the configured
   account, role, external ID, networks and provider reference against the old
   connection before changing ownership; a mismatch stops the cutover.

   Move platform units and their capacity operations, join credentials, machine
   enrollments, provider launches and capacity recoveries to the platform
   namespace. Audit worker admission records and every scope-bearing foreign
   key. Preserve unit and instance IDs, operation IDs, attempts, deadlines,
   release identities, terminal history and outstanding cleanup. Clear the
   customer `provider_connection_id` only on adopted platform units; the current
   internal-unit constraint already permits a platform unit without one.

   `capacity_recovery_ownership_fence` currently forbids workspace changes and
   edits to completed recovery rows. The migration must take exclusive locks,
   perform a narrowly enumerated ownership reassignment and restore the fence
   in the same transaction. Compare all non-ownership fields before and after.
   Do not weaken the runtime invariant to make migration pass.

   Treat credentials separately from metadata. Reissue machine and worker
   credentials through their real owners after draining the old workers. Revoke
   pending launch credentials or re-encrypt them through the owning cipher when
   their associated namespace changes. Never relabel encrypted data without
   verifying its key and authenticated context.

   Once all references and pending authorization work are accounted for, remove
   the old platform customer-connection row without invoking customer disconnect,
   which would tear down the adopted fleet. Remove platform-only fields and
   branches from the customer connection model. Preserve every BYO connection.

5. **Remove the token-dependent deployment path.** Replace the administrator
   bootstrap and HTTP fleet registration jobs with platform initialization and
   binding validation before workloads start. Validation must succeed without a
   running API and must fail clearly on missing permissions or mismatched
   infrastructure. The scheduler remains the owner that purchases capacity.
   Use the same initialization owner in Compose with local credentials.

   Remove the platform-only `ensure_fleet` public route, SDK method, HTTP request
   model and CLI transport path. Remove the platform branches of fleet teardown
   that enumerate a user's workspaces. Keep an explicit deployment-scoped
   operational command for fleet inspection and teardown, with exact targets.
   Update Helm ordering, settings, runbooks and public consumers in the same
   change. Update web Zod schemas if their contracts change. Compute settings
   must receive only genuine customer connections, not hide platform rows with
   a frontend filter.

   Remove `LAZYCLOUD_TOKEN` and administrator GitHub identity from deployment job
   bindings. Preserve the variables where interactive user tools still need
   them. After a successful deployment without that secret property, revoke the
   repair account credential and remove only its operator-secret
   property. Preserve all sibling secret properties.

## Acceptance

Use the cheapest owner evidence for authorization and migration invariants, then
one real local deployment and the production cutover. Required outcomes are:

- An empty local database initializes and starts workers before the first user
  exists. Creating the first administrator afterward enables human management.
- Revoking all human tokens and disabling the test administrator leaves service
  credential renewal, worker enrollment and platform capacity recovery working.
  Prove this on the isolated local deployment, not by disabling production users.
- Customer tokens cannot reach the platform namespace, adopt its fleet or issue
  its service credentials. Wrong AWS account, role, profile, instance or pool
  proofs are rejected. Existing BYO-cloud authorization remains scoped.
- A migration rehearsal preserves resource identities, pending recovery,
  fencing and cleanup with representative retained history. Another execution
  of initialization does not issue duplicate credentials or create duplicate
  resources. A configuration mismatch causes no adoption.
- The platform AWS account is absent from account Compute settings. A separate
  customer connection can still be connected, used and removed without changing
  platform resources.
- The exact release deploys without the operator account token. Fresh workers
  report the activated image and agent, a real image build and function invocation
  complete, and the gateway serves the workload path. Verify another sync and
  service restart, not only first initialization.

For changed directory and reconciliation reads, measure query count and returned
bytes with representative history before and after. Cover idle and active passes
at the actual replica count and cadence. Platform provider lookup must not scan
customer connections or account memberships. Use indexed namespace lookup and a
shared provider snapshot per pass. Verify the resulting deployed query rate;
if query insights are unavailable, report that acceptance gap.

Run changed owner checks after each coherent slice and the release gate before
merging the implementation. Add only focused tests for distinct authorization,
migration, fencing and cleanup invariants. Execute the real deployment scenario
instead of unit-testing its orchestration.

## Production cutover

Downtime and worker replacement are acceptable for this deployment. Use a
controlled cutover rather than retaining old ownership paths for compatibility.

1. Publish all artifacts. Record the exact database revision, platform resource
   IDs, active work, pending cleanup, secret consumers and current release.
2. Pause automatic sync, stop new admission, and drain or explicitly stop active
   work. Fence the old schedulers, API writers and workers before migration.
3. Run the forward migration and idempotent initialization. Compare the scoped
   adoption inventory. Do not reset the database or delete cloud resources as a
   substitute for migrating ownership.
4. Start the new services, resume sync and admit fresh workers under the platform
   namespace. Poll database state, both ends of the gateway, agent logs and AWS
   inventory together. Complete the public build and function acceptance.
5. Confirm no deployment consumer reads the human token, remove that dependency's
   secret property and revoke its exact token ID. Clean only resources created
   for acceptance. Record the final provider and workload state.

Before the ownership migration, the previous release can resume with its existing
credential. After ownership changes, roll forward with the new model; old code
must not write into the migrated database. Any restore would require a separately
reviewed database and provider-state recovery, not an automatic image rollback.
