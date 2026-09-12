# Deployment

Compose files, container images, provisioning assets, and the ingress that puts
the platform on a network.

Root `compose.yaml` is the canonical local stack. It stays aligned with the
Pydantic settings that read it, provides the real backing services rather than
substitutes, and gives every service an explicit owner and health check.
`README.md` here and the subdirectory READMEs are the operator runbooks.

- Never commit secrets, generated credentials, or local state. A value that
  identifies a resource may live here; a value that authenticates to one may
  not, and belongs in a file the deployment points at.
- Owner-authorized platform infrastructure and deployment work uses AWS profile
  `default`. Customer BYO-cloud acceptance uses `default-test`. A failure to assume
  the test operator role does not block a platform deployment or authorize IAM
  repairs. GitHub deployment jobs retain their configured OIDC identity.
- Deployed databases are persistent. Append Alembic revisions; never rewrite
  the baseline or reset production data during an upgrade.
- Run migrations before starting new application pods. Previous replicas keep
  serving during migration, so new ownership columns must preserve their reads
  and valid writes. Test that older writers cannot erase ownership recorded by
  the new repository.
- A deployment value is usually read on several independent paths, so correcting
  one place proves nothing about the rest. When a name, origin, or credential
  changes, find every consumer of it in the same change.
- A stack that reports healthy is not a stack that works. A health check
  describes a process, not the path through it. Confirm the boundary you changed
  from end to end rather than trusting aggregate status.
- Sidecars that share another service's network namespace are destroyed when
  that service is recreated, and the stack will not say so. Treat the lifetime
  relationship as part of the change, not as something to rediscover.
- The Compose agent and its worker share the host network namespace. User
  containers reach the runtime through its private WireGuard address. Keep this
  path in local acceptance: host HTTP overrides bypass tunnel forwarding and can
  report success while production function calls cannot reach the runtime.
- Two workers share this host and nothing coordinates them: the `container-worker`
  service is the shared platform fleet, and the `agent` service runs one machine a
  customer account joined. Each allocates container addresses inside its own
  control-plane scope, keyed on its own machine id, so a bridge name or subnet used
  twice is two allocators issuing one address with no lock between them. Machine
  fingerprint, state directory, pool, and bridge are the four values that must
  differ, and none of them fails visibly when it does not.
- A release and a deploy are separate workflows, chained by `ship.yml`. They are
  not merged because a release is public and immutable: a customer's own AWS
  account resolves the agent binary, worker image, and exact node AMI IDs out of
  its manifest. Host AMIs have a separate recipe and workflow, so application
  releases never rebuild them. Sequencing release and deploy is the part that
  cannot be left to chance. Both once triggered on `v*` independently, so a
  tagged deploy raced the release it was meant to run. Ship is dispatched with
  a bump choice, pushes the tag itself, and passes the version to both as an
  input; a tag pushed with the workflow token starts no other workflow.
- One complete manifest selects platform images, the worker image, agent binary,
  and host catalog. Ship publishes it and records it for Argo. Argo health checks
  precede the PostSync active-release ConfigMap. API and scheduler read its mounted
  file to admit current artifacts and previously verified workers. Every replica
  serves the activated release; only its matching build authorizes upgrades.
  PostgreSQL retains verified worker identity and unfinished updates across Redis
  loss. Managed and joined agents update in place through their supervised service
  after work drains. The maintenance slot remains occupied until the target worker
  has fresh request intake. Host replacement is reserved for host lifecycle changes.
- The network image records its executable linux/amd64 manifest digest, not its
  commit tag or attestation index. Ship selects it from the same published
  commit automatically. Its Docker build copies only the installed network
  workspace dependencies, so an unrelated API edit does not restart WireGuard.
  Network source or dependency changes publish and select a new image without an
  operator-maintained release pin.
- Helm owns application settings and secret property bindings. Terraform publishes
  non-secret resource identities in a versioned infrastructure descriptor; app CI
  reads that document, never Terraform state. Git records the descriptor snapshot,
  environment settings, image digests and one release URL on the deployment branch.
- CI builds and records; Argo installs. Ship publishes images and Deploy
  commits the values naming them to the deployment's branch, and stops. Nothing in
  CI runs `helm install`, because a workflow that installs and a controller that
  reconciles are two opinions about what should be running, and they disagree
  where nobody is looking.
- An image belongs to a commit, not to a deployment. Ship builds a commit
  once into repositories every deployment shares, tagged by the commit, and a
  deployment names its executable digests. Promotion selects staging's complete
  release for prod and builds nothing; what crosses is one manifest URL,
  and prod's values are rendered from prod's own infrastructure descriptor.
- The list of deployments lives on `main`; what each runs lives on its branch.
  Argo's root Application reads `deploy/argocd/apps` on `main`, one file per
  deployment naming its namespace and branch, so adding a deployment is
  merging its file and a change there takes effect on the next sync with no
  deploy between. The chart and its values come only from the branch.
- Operator documentation is part of the change: when deployment behavior
  changes, the runbook that describes it changes with it.
