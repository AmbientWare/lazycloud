# Deployment configuration

Terraform owns resource identities, networks, IAM, secret documents and the
database server ceiling. It publishes a non-secret infrastructure descriptor to
the deployment bucket. Only Terraform writes this document. The deploy role can
read that object and push images, but cannot read Terraform state or write S3.

Helm owns application defaults, environment policy, fleet ceilings and secret
property bindings. Edit `chart/values.yaml` or `chart/environments/prod.yaml` and
deploy. No infrastructure apply is needed for those changes. Runtime processes
receive environment variables and mounted files, never Terraform output files.

Postgres owns customer configuration and workload state. Redis owns coordination.
The immutable release manifest owns agent artifacts, worker image digests and host
AMI identities. It is selected in the deployment branch, alongside configuration
and control-plane images. The CLI and application do not pick a newer release
from S3. Customer-connected accounts remain database-owned; Helm governs only
the platform fleet. Fleet ensure refuses a changed account, role, external ID or
network instead of replacing or adopting the existing connection.

## Deploy sequence

Deploy captures the current branch revision and the infrastructure descriptor,
validates the descriptor and environment values, and renders the exact Helm
chart. It verifies the selected release's agent artifact and worker image before
building control-plane images. Only after all images exist does it publish the
configuration and release together, using a compare-and-swap branch push.
The selected release must contain the same managed-package sources as the
control plane. Changes to those packages require Ship to publish matching worker
artifacts; a code-only deploy may retain the release when those sources match.
Preflight also checks overlapping old and new database pools and refuses an old
chart with undeclared pools. Failure before the push leaves the selected deployment unchanged. A partially
published image set fails explicitly because commit tags are immutable.

CI authenticates to ECR Public before inspecting the worker image. Its deploy
role receives token-issuance permissions, not public-registry publishing rights.
AWS documents the required permissions in
[ECR Public authentication](https://docs.aws.amazon.com/AmazonECR/latest/public/public-registry-auth.html).
Authentication failures stop preflight; they do not mean an image is missing.

Cloudflared configuration and the AWS role-chain ConfigMaps have pod-template
checksums. A change to a mounted configuration therefore rolls its consumers.

## First transition on the existing installation

Keep production changes stopped until an operator approves this checklist.

1. Resolve the PlanetScale provider/state discrepancy. Earlier plans reported
   existing database resources as deleted. Do not accept a replacement plan,
   refresh-only deletion, state-only output rewrite, or `-refresh=false` apply
   as a substitute for resolving that discrepancy.
2. Review the core plan and apply the scoped Argo pause ownership change. Verify
   that a child pause survives root reconciliation before relying on it.
3. Remove application-only variables from the deployment tfvars. The control-plane
   service-account variable is now a named object; its default names and existing
   Pod Identity association keys are unchanged. Review the deployment plan.
   Expected changes are descriptor publication, deploy IAM permissions and secret
   description metadata, not database, network, identity or secret-value replacement.
4. Set GitHub environment variable `INFRASTRUCTURE_CONFIG_URI` from the deployment
   output. Keep `AWS_DEPLOY_ROLE_ARN` for OIDC. Deploy no longer uses `TF_STATE_BUCKET`.
5. Check the old and proposed database budgets before the first rollout. The old
   gateway did not declare a pool and could allocate up to 15 connections per
   process. This chart explicitly limits its serial database work to one. With
   the current replica counts, the target chart budgets 39 connections against
   the unchanged server ceiling of 40, including overlap and an operator reserve.
   Preflight intentionally refuses the old undeclared gateway pool. First narrow
   the gateway pools in the deployment branch, retaining the running image and
   all unrelated values, and let Argo apply that configuration-only change.
   Inspect database sessions, pod revisions and scheduler progress before proceeding.
6. Run the deploy preflight, then approve the application rollout. Verify build
   completion, running-container continuity and warm placement latency across a
   control-plane and scheduler replacement. Inspect durable records, worker and
   API logs, queue depth and the provider's machine inventory each cycle. Stop on
   growing pending work rather than waiting for a timeout.

Schema changes are not part of this PR. Do not reset production data. The
configuration-only gateway preparation is a deployment-branch operation using
the existing binary, not a second application release.

## Credential changes

Add a property binding under `secrets.map` and include it only in the required
consumers under `environment`. Preserve unrelated properties when updating the
operator-managed document. Terraform continues to own the platform document;
the WireGuard bootstrap owns its key document.

For rotation, refresh External Secrets first. Poll its Ready condition and refresh
time and the destination Secret's resource version without printing its contents.
Then bump the affected `secretRevisions` values and deploy. Existing environment
variables and subPath mounts do not refresh themselves. Test the authenticated
operation after rollout. Keep the predecessor credential valid during overlap
where the provider supports it. Database credentials affect API, scheduler,
gateway and bootstrap jobs; cache-token changes affect cache, API and scheduler.

Changing WireGuard server keys requires a separate peer migration. Do not rotate
them by editing the document or bumping a Helm revision.

## Pause and resume

After the core ownership change has been applied, pause the child Application:

```sh
kubectl -n argocd patch application lazycloud-prod --type merge \
  -p '{"spec":{"syncPolicy":{"automated":{"enabled":false}}}}'
```

Poll the child field, root sync status, active operation and pod revisions in
short cycles. The field must remain false across root reconciliation. If a sync
is already running, terminate it through the authenticated Argo CLI or UI and
confirm its operation phase changed. An autosync pause alone does not terminate
an active operation.

Argo termination does not undo already-applied Kubernetes objects. Deployment
and StatefulSet controllers can continue replacing pods. Inspect their revisions
and replica counts before deciding whether to freeze a controller or roll back.
Do not report a deployment stopped while its Kubernetes rollout is still active.

Resume only after reviewing the recorded branch and current workload health:

```sh
kubectl -n argocd patch application lazycloud-prod --type merge \
  -p '{"spec":{"syncPolicy":{"automated":{"enabled":true}}}}'
```

Resume can immediately apply the branch's recorded release. Pausing does not
cancel queued GitHub workflows, so stop those separately if no new deployment
record should be published.
