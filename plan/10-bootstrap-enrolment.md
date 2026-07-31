# Bootstrap, Enrolment and Networking

## Target state

A managed-pool node boots from ~20 lines of user-data, not ~500. The script does
two things: resolve its provider identity from the link-local metadata service,
then `curl` the control plane's published `/install/agent` provisioner and `exec`
it with the provider-enrolment flags it already accepts. Nothing else. That
provisioner — not user-data — installs Docker, installs the digest-pinned
Tailscale build, installs the agent, runs `install-service`, and waits for
enrolment with real systemd and journal diagnostics.

The agent owns everything after that, in Python, over the public control-plane
origin. It mints the presigned STS `GetCallerIdentity` proof it already knows how
to mint, POSTs `/gateway/provider-nodes/enroll`, receives an agent token, then
calls `/gateway/agents/transport-credential` for a single-use, machine-scoped
tailnet key and joins the tailnet as `lazycloud-agent-<machine_id>-g1` — its
first and only identity. Phase and failure reports go over the same HTTPS origin
from the first second of the agent's life, so a node that fails to install
Docker, fails to reach Tailscale, or fails to join reports the reason instead of
disappearing.

The tailnet carries the data path only: the control plane dialing the agent's
route proxy. There is no pool-scoped tailnet credential, no bootstrap tag, no
pre-enrolment tailnet identity, no identity rotation on the happy path, no
`tailscaled` systemd unit written by user-data, and no SigV4 implementation in
shell.

## Why this is simpler and more reliable

- **Three of six bootstrap failure reasons are unreportable today.** `report()`
  dials the tailnet origin (`packages/compute/src/compute/node_bootstrap.py:231-242`)
  and `CURL_RESOLVE` is only populated inside `tailnet_join`
  (`node_bootstrap.py:442`). The ERR trap maps `STEP=identity|tailscale|tailnet`
  to reasons (`node_bootstrap.py:208-215`) that can never reach the control
  plane. `bootstrap_timed_out` naming nothing is the designed outcome for the
  three most likely failures.
- **The precondition already exists.** `validate_provider_network_configuration`
  requires a valid HTTPS `gateway_origin` for exactly the `Remote` network class
  that managed pools use (`packages/networking/src/networking/settings.py:161-167`,
  `packages/providers/aws/src/provider_aws/account_connection.py:563-570`).
- **The node already has public internet from second zero.** It fetches
  `pkgs.tailscale.com` (`node_bootstrap.py:321`), a public S3 agent URL
  (`node_bootstrap.py:343`), `dnf` repos (`node_bootstrap.py:303`) and a public
  container registry, and launches into a public subnet with an Internet Gateway
  (`account_connection.py:1338-1378`). What it lacks is a publicly addressable
  control plane, not a network.
- **The simpler design already ships in the same package.** Direct AWS machines
  use 30 lines of user-data that curl the public `/install/agent`
  (`packages/providers/aws/src/provider_aws/provider.py:395-396` →
  `packages/providers/aws/src/provider_aws/ec2.py:343-351` →
  `packages/compute/src/compute/bootstrap.py:37-66`).
- **The managed-pool script is a less-instrumented re-implementation of a
  provisioner that already ships, and that already accepts the provider-enrolment
  flags.** This is the strongest argument in the track. `/install/agent`'s `main()`
  (`packages/agent/src/agent/operations.py:256-270`) runs `ensure_docker`
  (`:490-525`), `ensure_tailscale` (`:571-614`, digest-verified per arch),
  `install_agent`, then `run_agent` — which ends at `install-service --target
  systemd` and `wait_for_agent_service` (`:710-716`). It already parses
  `--provider-enrollment-request`, `--provider` and `--provider-instance-identity`
  and forwards them (`:696-701`). Its post-install watch reports `systemctl show`
  plus 80 lines of redacted `journalctl` on failure (`:745-761`, redaction
  at `:763-773`); the managed
  script's hand-added equivalent reads only `systemctl show -p Result`
  (`node_bootstrap.py:508-517`). So the managed path is not merely duplicating
  install steps — it is a strictly worse copy of a complete node provisioner that
  the control plane already serves.
- **The agent already implements every step after that.** `_enroll_provider_node`
  (`apps/agent/src/agent_app/daemon.py:1070-1121`) mints the proof and enrols;
  `_start_tailnet` (`daemon.py:1251-1296`) fetches the machine key and
  authenticates with the returned `control_url`; the phase-report client method
  exists and is never called (`daemon.py:360-366`).
- **One security-critical signature, not two.** `managed_pool.py:1158-1213` is 56
  lines of hand-rolled SigV4 in bash duplicating
  `packages/providers/aws/src/provider_aws/provider_node_proof.py:161-180`.
- **Churn signal.** 14 commits in eight weeks touch this path, most of them
  fixing the tailnet-first ordering (`git log packages/compute/src/compute/node_bootstrap.py`).

### Settled by inspection — recorded so BOOT-05 and BOOT-08 are not re-litigated

- **`install-service` does not install Tailscale.** There is no download or
  install logic in `apps/agent/src/agent_app/main.py`; it accepts only binary-name
  arguments (`:250-251`, `:425-428`) and hands them to the unit. Any design that
  curls the bare agent binary and execs `install-service` leaves the node with no
  `tailscaled` and therefore no data path. Only `/install/agent` installs it.
- **`ensure_docker` and `ensure_tailscale` are genuine no-ops on a baked AMI, so
  BOOT-08 does not regress boot time.** `ensure_docker` returns immediately when
  `docker_ready` succeeds (`operations.py:494-496`, `docker_ready` at `:457-459`),
  and `ensure_tailscale` returns immediately when the installed client *and*
  daemon both report the pinned version (`:572-574`, `tailscale_ready` at
  `:557-562`). The bake `systemctl enable --now docker` (`deploy/ami/bake.py:627`),
  so Docker is running at boot on a baked image. Neither path downloads anything
  when the dependency is already present and pinned.
- **The `auto` defaults never silently skip an install.** `INSTALL_DOCKER` and
  `INSTALL_TAILSCALE` both default to `auto` (`operations.py:239-240`), and `auto`
  is in the allow-set of `docker_install_allowed` / `tailscale_install_allowed`
  (`:461-466`, `:564-569`). Every other branch fails loudly and non-zero: refused
  installation (`:507-509`, `:575-577`), missing root (`:513-515`, `:578-580`),
  unsupported host (`:539-541`), digest mismatch (`:657-674`), or a post-install
  readiness check that still fails (`:519-524`, `:611-613`). There is no path on
  which a managed node proceeds without a running Docker and a pinned Tailscale.
  Managed nodes must therefore pass neither flag — the defaults are correct.
- **One supply-chain asymmetry to note, not to fix here.** The Tailscale archive
  and the agent artifact are both SHA-256 pinned (`:598`, `:657-674`), but the
  non-Amazon-Linux Docker path pipes `https://get.docker.com` unverified
  (`:545-554`). Managed nodes are Amazon Linux 2023 and take the pinned `dnf`
  branch (`:528-538`), so this does not affect this track — but it is a real gap
  for any other host OS and belongs to whoever owns the installer's threat model.

## Dependencies on other tracks

I am asserting three cross-track dependencies. Two are hard blockers.

1. **INFRA (13-production-readiness) — public control-plane ingress. HARD BLOCKER
   for BOOT-04 onward.** `LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL` defaults to
   `http://127.0.0.1:8000` (`compose.yaml:23`), and the Funnel that
   `deploy/tailnet-gateway/serve.json:15-17` requests is not granted by
   `deploy/tailnet/policy.json.tftpl` (no `nodeAttrs`/`funnel` entry anywhere in
   `deploy/tailnet/`). BOOT-04 through BOOT-09 cannot land until a genuinely
   public, TLS-terminated origin exists that an EC2 instance in an arbitrary
   customer account can reach. Expected ID: INFRA-01 or equivalent.
2. **INFRA (13-production-readiness) — rate limiting and bounded verification on
   `/gateway/provider-nodes/*`. HARD BLOCKER for BOOT-04.** Each request triggers
   a synchronous outbound STS call with a 3 s timeout inside an API worker
   (`packages/providers/aws/src/provider_aws/provider_node_identity.py:298-308`,
   `:29`). Exposed publicly and unauthenticated, that is an amplification
   primitive against the control plane's worker pool. See Open Questions.
3. **ERR (12-failure-semantics) — ownership of the terminal-vs-reclaimable
   boundary.** BOOT-01 gives `MachineBootstrapPhase.Failed` a reclaim deadline.
   If 12 owns the terminal-state model, BOOT-01 must agree with it rather than
   contradict it. Soft: BOOT-01 is correct standalone, but the two must not
   define `Failed` differently.
4. **CAP (11-capacity-scheduling) — replacement launch after reclaim.** A `Failed`
   node holds the ASG's only slot and no replacement launches
   (`plan/00-prior-tailnet-plan.md:118-121`). BOOT-01 makes the reclaim happen;
   CAP must confirm the replacement launch follows and that
   `max_launch_attempts` degradation still bounds it
   (`packages/compute/src/compute/service.py:2634-2662`). Soft.

## Work items

- [ ] **BOOT-01** Give the `Failed` bootstrap phase a reclaim deadline
  - **Files**: `packages/compute/src/compute/reclaim.py:43-48`;
    `packages/compute/src/compute/service.py:5119-5132`
  - **Change**: add `MachineBootstrapPhase.Failed.value: 300` to
    `DEFAULT_BOOTSTRAP_PHASE_DEADLINE_SECONDS`. In
    `_provider_bootstrap_failure_to_reclaim`, add `MachineBootstrapPhase.Failed`
    to the early-return set at `service.py:5119-5123` so a node that reported a
    failure is reclaimed under the reason it reported, not re-diagnosed as
    `BootstrapTimedOut`. Read `record.bootstrap_failure_reason` and return it.
    Also delete the "leaving instance available for inspection" behaviour that
    depends on the node surviving: `node_bootstrap.py:217` keeps the message but
    the node is now reclaimed, so update the message to say the node will be
    reclaimed. Today `phase_deadline_for` returns `None` for `Failed`
    (`reclaim.py:102-113`) and `Failed → Deleting` is the only legal transition
    (`service.py:281`), so a node that successfully reports its own failure runs
    and bills forever while a silently-failed node is reclaimed in five minutes.
  - **Risk**: a node is terminated before an operator can inspect it. That is the
    intended trade — inspection is impossible anyway (no console output, no SSM,
    read-only credentials, `plan/00-prior-tailnet-plan.md:122-125`). Blast radius:
    managed pools only; direct machines have no bootstrap phase record.
  - **Acceptance**: with a pool at one desired worker, force a node to report
    `bootstrap-failure`; observe
    `compute_provider_instances.payload->>'bootstrap_failure_reason'` retain the
    reported reason, the instance reach `Terminating`, and a replacement instance
    launch within one reconcile cycle. Add one focused case to
    `packages/compute/tests/test_provider_capacity_reclaim.py` asserting a
    `Failed`-phase record past deadline is reclaimed under its own reason —
    this is a durable state transition with direct cost impact, not proven by the
    existing `BootstrapTimedOut` case at `:1528`.
  - **Depends on**: none (coordinate wording with ERR/CAP)

- [ ] **BOOT-02** Stop a revoked agent from re-enrolling forever
  - **Files**: `apps/agent/src/agent_app/daemon.py:783-790, 812-816`;
    `packages/agent/src/agent/service_manager.py:396-427`
  - **Change**: when `agent_authority_was_revoked` fires, persist a terminal
    marker in the agent state directory and delete `agent-state.json`, then exit
    non-zero. On startup, refuse to run when the marker is present and exit
    non-zero with the reason. Change the rendered unit from `Restart=always` /
    `StartLimitIntervalSec=0` (`service_manager.py:406, 417-419`) to
    `Restart=on-failure` with a start limit that permits transient gateway
    failures but stops a revoked agent: today the agent exits 0 on revocation,
    systemd restarts it 15 s later, `resolve_identity` finds intact saved state
    and re-joins, and it is re-rejected — indefinitely, on a billing machine.
    Preserve the comment's real intent (a transient gateway failure must keep
    retrying); the distinction is that revocation is not transient.
  - **Risk**: a start limit that is too tight turns a slow control plane into a
    permanently dead agent — the exact failure the current comment warns about.
    Size the limit against the bootstrap phase deadline (300 s) so an agent
    survives a full deadline window of gateway unavailability. Blast radius: every
    agent on every pool, private and managed.
  - **Acceptance**: on a live node, revoke the machine at the control plane and
    observe the unit reach `failed` and stay there, with no further
    `/gateway/agents/join` entries in the control-plane access log for that
    machine. Add one focused daemon case proving a persisted revocation prevents
    re-enrolment on the next start — a durable transition and a cost-bearing
    cleanup obligation. Do **not** test the rendered unit text; that is generated
    command shape.
  - **Depends on**: none

- [ ] **BOOT-03** Report bootstrap phases from the agent, not only from user-data
  - **Files**: `apps/agent/src/agent_app/daemon.py:360-366` (client method,
    currently unused), `daemon.py:1070-1121` (`_enroll_provider_node`),
    `daemon.py:740-760` (run sequence around `_start_tailnet`)
  - **Change**: call `record_provider_node_bootstrap_phase` from the agent at
    three points, each with a freshly minted proof (a proof is single-use —
    `provider_node_identity.py:268-282` — so reuse would be rejected as a replay;
    `_enroll_provider_node:1108-1109` already re-mints for its failure report):
    `Booting` immediately at daemon start before enrolment, `Joining` after a
    successful enrol, `Ready` once the runtime-ready marker is written. Extend the
    existing failure report at `daemon.py:1105-1120` to cover the pre-enrolment
    phases with the matching `MachineBootstrapFailureReason`
    (`RuntimeInstallFailed`, `NetworkJoinFailed`, `ProviderIdentityFailed`). This
    lands **before** any shell reporting is deleted, so there is never a window in
    which a node cannot report.
  - **Risk**: duplicate phase reports while the shell also reports. Harmless —
    `record_provider_bootstrap_status` is a phase-transition write with a legal
    transition table (`service.py:245-283`), and a repeat of the current phase is
    a no-op. Blast radius: managed pools only.
  - **Acceptance**: run `python -m tests.e2e.external.aws.one_machine_readiness`
    and observe `compute_provider_instances` phase transitions carrying
    `bootstrap_observed_at` timestamps that follow the agent's own log timeline,
    not the user-data script's. No new unit test: the live scenario is the
    authoritative owner and cheaper than reconstructing it.
  - **Depends on**: none

- [ ] **BOOT-04** Point managed-pool nodes at the public control-plane origin
  - **Files**: `apps/api/src/api/server/services.py:785-811`;
    `apps/scheduler/src/scheduler_app/services.py:249-276`;
    `packages/gateway/src/gateway/pool_bootstrap.py:158, 209`
  - **Change**: pass the public gateway origin, not the internal tailnet origin,
    as `PoolBootstrapProvisioner.control_plane_url` in both composition sites.
    Both must change in the same commit or the API and the scheduler will
    alternate launch-template versions — the exact hazard
    `pool_bootstrap.py:196-201` documents. Leave `gateway_runtime_http_url`
    (worker-facing, tailnet) untouched.
  - **Risk**: if the public origin is not genuinely reachable from an arbitrary
    customer VPC, every node fails its first report and the pool never launches.
    This is the item that consumes the INFRA blocker and it must not land before
    it. Blast radius: all managed pools; direct machines already use the public
    origin.
  - **Acceptance**: from an EC2 instance in the test customer account with no
    tailnet session, `curl -fsS <public origin>/health` succeeds; then
    `python -m tests.e2e.external.aws.one_machine_readiness` reaches `ready: 1`
    with the node still joining the tailnet through the old shell path.
  - **Depends on**: BOOT-03, INFRA public ingress, INFRA rate limiting

- [ ] **BOOT-05** Reduce the bootstrap script to identity plus the published provisioner
  - **Files**: `packages/agent/src/agent/operations.py:203-270, 272-368, 616-634`;
    `packages/compute/src/compute/node_bootstrap.py:57-73, 82-88, 127-135, 173-521`;
    `packages/providers/aws/src/provider_aws/managed_pool.py:1140-1251`
  - **Change**: one atomic commit — the script must not stop joining the tailnet
    before the agent starts doing it, and the flag added in the first step must
    exist before the script references it.

    *First*, add an `--agent-url` argument to `build_agent_install_script`
    (parsed alongside `--agent-version` / `--agent-sha256` at
    `operations.py:272-368`, consumed in `install_agent` at `:616-634`) so the
    caller can name the artifact source directly. Without it, `install_agent`
    fetches from `$GATEWAY/install/agent/$OS_NAME/$ARCH_NAME` (`:628-630`), which
    moves a **47.6 MB** binary (`dist/agent-binarys/*/manifest.json`,
    `size_bytes`) onto the control-plane ingress on every scale-out — the exact
    class of transfer that broke the previous public ingress. Managed nodes keep
    fetching from the public release bucket as they do today
    (`node_bootstrap.py:343`); the sha check in `verify_sha256` (`:657-674`) is unchanged
    and stays mandatory.

    *Then* rewrite the user-data template. `bootstrap_main` (`:455-518`) becomes
    `resolve_node_identity` → `curl -fsSL "$CONTROL_PLANE_URL/install/agent" | sh
    -s -- --gateway "$CONTROL_PLANE_URL" --provider-enrollment-request … --provider
    aws --provider-instance-identity imds-v2 --machine-fingerprint … --hostname …
    --executor container --worker-image … --max-gpus … --agent-version …
    --agent-amd64-sha256 … --agent-url …`. Delete from the template: `report`,
    `report_phase`, `report_failure` (`:231-250`), `ts()` (`:252-254`),
    `parse_control_plane_origin` (`:256-277`), `docker_ready` / `ensure_docker`
    (`:279-281, 295-313`), `tailscale_ready` / `ensure_tailscale` (`:283-288,
    315-336`), `agent_ready` / `ensure_agent` (`:290-293, 338-350`),
    `ensure_tailnet_service` (`:357-393`), `is_tailnet_address` (`:398-406`),
    `tailnet_join` (`:408-453`), the `CURL_RESOLVE` variable, the post-handoff
    watch loop (`:508-517`), and the tailnet unit/socket/state constants
    (`:57-66`, `:72-73`). Every one of those is owned by the provisioner already.
    Do **not** pass `--tailnet-mode sidecar` or `--tailnet-socket`; the agent uses
    its default Managed mode (`apps/agent/src/agent_app/main.py:246`) and owns its
    daemon. Drop `tailnet_auth_key` from `NodeBootstrapSettings` (`:133`) and its
    substitution (`:559`); keep `tailscale_version` / `tailscale_sha256` only if
    the provisioner does not already pin them — it does (`operations.py:557-561`),
    so drop those too. Remove `report_identity_fields()` from
    `_REQUIRED_PROVIDER_SYMBOLS` (`:82-88`).

    In the AWS fragment, delete `EMPTY_PAYLOAD_SHA256`, the credential variables,
    `sha256_hex`, `hmac_hex`, `uri_encode`, `mint_proof` and
    `report_identity_fields` (`managed_pool.py:1141-1149, 1158-1213, 1239-1242`)
    and reduce `resolve_node_identity` (`:1215-1237`) to fetching the IMDSv2
    token, instance id and region. Keep `node_fingerprint`, `node_hostname`,
    `PROVIDER_INSTALL_FLAGS`, and `imds()`.

    In `daemon.py:1274-1281`, keep the identity-mismatch branch — it still guards
    control-plane divergence — but correct the comment, which now describes a pool
    bootstrap identity that no longer exists.
  - **Risk**: the highest-risk item in the track. `install-service` **does not**
    install Tailscale — there is no download or install logic anywhere in
    `apps/agent/src/agent_app/main.py`, only binary-name arguments (`:250-251`,
    `:425-428`). So a user-data script that curls the bare agent binary and execs
    `install-service` would leave the node with no `tailscaled` and therefore no
    data path. Routing through `/install/agent` is what makes this item correct,
    not a stylistic preference. Second risk: the node now depends on the control
    plane serving the installer script before it has an agent; that is a few tens
    of KB and the same dependency direct machines already carry
    (`compute/bootstrap.py:54-55`), but it does mean a control-plane outage blocks
    scale-out — which was already true, since enrolment blocks it anyway. Blast
    radius: every managed-pool node.
  - **Acceptance**: `python -m tests.e2e.external.aws.one_machine_readiness`
    reaches `ready: 1`, and the tailnet device list shows the node appearing
    **once**, directly as `lazycloud-agent-<machine_id>-g1`, with no
    `bootstrap-i-*` device at any point. Confirm from the node's cloud-init log
    that `ensure_docker` and `ensure_tailscale` took their early-return paths on a
    baked AMI (`operations.py:494-496`, `:572-574`) and downloaded nothing.
    Separately, break the `--agent-url` for one launch and observe
    `wait_for_agent_service`'s journal diagnostics (`:723-761`) in the cloud-init
    log plus a reported failure reason, rather than `bootstrap_timed_out`. Prune
    `packages/compute/tests/test_node_bootstrap.py` and
    `packages/providers/aws/tests/test_provider_aws_bootstrap_script.py` to the
    assembler-contract assertions only (sentinel substitution, `shlex.quote`,
    unresolved-placeholder scan, `bootstrap_main` as the literal last line); every
    row asserting deleted shell behaviour fails the Test Decision Gate once that
    behaviour is gone. Note `test_provider_aws_bootstrap_script.py:78` currently
    asserts `"/install/agent" not in script` — that assertion inverts.
  - **Depends on**: BOOT-04

- [ ] **BOOT-06** Delete the pool bootstrap key subsystem
  - **Files**: `packages/gateway/src/gateway/pool_bootstrap.py:1-143, 156-215`;
    `packages/database/src/database/repositories/compute.py:1152-1211`;
    `packages/database/src/database/tables/compute.py:402-418`;
    `packages/shared/src/shared/compute_enrollment.py:88-111`;
    `packages/networking/src/networking/tailnet_control.py:26-30, 183, 189-196,
    235-247, 373-408`; `packages/networking/src/networking/settings.py:72, 79-88,
    100-127`; `packages/compute/src/compute/providers.py:65-70`;
    `packages/providers/aws/src/provider_aws/managed_pool.py:81-84`;
    `packages/compute/src/compute/service.py:172-189, 1560, 1640, 2395, 2558,
    3729-3750, 3753-3780`;
    `packages/database/src/database/alembic/versions/0001_current_schema.py`
  - **Change**: delete `PoolBootstrapCredentialService` (`pool_bootstrap.py:45-143`)
    and rewrite the module docstring; keep `PoolBootstrapProvisioner` and
    `pool_bootstrap_provisioner`, which still supply the origin, the pinned agent
    artifacts and the worker image. Delete `PoolBootstrapCredentialRepository`,
    `PoolBootstrapCredentialTable`, `PoolBootstrapCredential`, and the
    `compute_pool_bootstrap_credentials` table from the Alembic baseline (the repo
    is predeployment; update the baseline and re-bootstrap, do not add a
    revision). Delete `issue_pool_bootstrap_key`, `pool_bootstrap_tag`,
    `pool_bootstrap_key_ttl_seconds`, `pool_bootstrap_key_refresh_seconds`,
    `DEFAULT_POOL_BOOTSTRAP_*`, `issuable_tags`, and
    `bootstrap_tag_must_be_distinct`; `validated_tags` returns two tags. Delete
    the `tailnet_auth_key` field from `ProviderPoolBootstrap`,
    `AwsManagedPoolBootstrap` and `NodeBootstrapSettings`. Delete
    `ProviderPoolBootstrapFactory.release` and `_release_pool_bootstrap` with its
    two call sites, and the now-dead `writes_launch_template` parameter from the
    protocol and all six sites — it exists only to gate key minting
    (`pool_bootstrap.py:178`).
  - **Risk**: `PoolBootstrapProvisioner.release` disappearing means pool deletion
    no longer has a credential hook. Confirm nothing else hangs off it before
    deleting the protocol method. `TailnetCleanupCoordinator` keeps its
    per-machine responsibility untouched. Blast radius: pool creation and deletion
    in both the API and the scheduler.
  - **Acceptance**: create and delete a managed pool end to end; confirm no row
    is written to any bootstrap-credential table (the table is gone), the
    launch-template user-data contains no `tskey-`, and
    `python -m tests.e2e.external.aws.one_machine_readiness` still reaches
    `ready: 1`. Delete every test that constructs a `PoolBootstrapCredential`.
  - **Depends on**: BOOT-05

- [ ] **BOOT-07** Remove the bootstrap tag from the tailnet policy
  - **Files**: `deploy/tailnet/policy.json.tftpl:5, 18-22, 30-33, 42-44, 46-56`;
    `deploy/tailnet/variables.tf:56-65`; `deploy/tailnet/main.tf:5, 23-24, 36`;
    `deploy/tailnet/outputs.tf:23`; `deploy/tailnet/terraform.tfvars.example:9`;
    `deploy/tailnet/README.md`
  - **Change**: delete the `bootstrap_tag` variable, its `tagOwners` entry, its
    grant, and its rows in the policy `tests` block; remove it from the OAuth
    client's `tags` list (`main.tf:36`) and from the three-distinct-tags
    precondition (`main.tf:23-24`). Two tags, two grants. Also delete `--ssh`
    from the bootstrap join — it is already gone with BOOT-05, but confirm no
    `ssh` block is introduced to compensate: none exists today, so the flag
    grants nothing.
  - **Risk**: changing the OAuth client's tag list **replaces the client**
    (`deploy/tailnet/README.md`), so `LAZYCLOUD_TAILNET_OAUTH_CLIENT_ID` and
    `_SECRET` must be re-exported to the deployment secret manager in the same
    change or every key issuance fails. Apply against the live tailnet only with
    a reviewed plan; this module owns the whole policy and must preserve unrelated
    users, devices, grants and keys.
  - **Acceptance**: `terraform -chdir=deploy/tailnet plan` shows only the intended
    removals and the client replacement; after apply, the policy `tests` block
    passes at Tailscale, no device carries the bootstrap tag, and
    `python -m tests.e2e.external.aws.one_machine_readiness` reaches `ready: 1`
    with re-exported credentials.
  - **Depends on**: BOOT-06

- [ ] **BOOT-08** Collapse the AMI bake onto the published install script
  - **Files**: `deploy/ami/bake.py:614-660, 665-680`
  - **Change**: replace `_BAKE_USER_DATA_TEMPLATE` — which independently installs
    Docker, the pinned Tailscale build and the agent — with a fetch of the control
    plane's own `/install/agent`, so the install logic has one owner. An
    install-only mode does not exist today and must be added: `main()` runs the
    three install steps and then unconditionally calls `run_agent`
    (`operations.py:256-270`), which requires join or provider-enrolment
    credentials the bake instance has no business holding, and `--foreground`
    only changes *how* it runs the agent (`:710-720`), not whether. Add an
    explicit `--install-only` argument that returns after `install_agent`, and
    have `validate_input` reject it combined with any credential argument.
    That is a real capability the installer lacks, not a shim. The bake instance
    is already on the public internet and already pulls the worker image
    (`bake.py:655`).
  - **Risk**: the bake is a release-time step; a break blocks a release rather
    than production traffic. The baked AMI must still satisfy the bootstrap
    script's guarded no-op checks or every node reinstalls at boot and the bake
    buys nothing.
  - **Acceptance**: bake one region, launch a pool from the resulting AMI, and
    observe boot-to-`ready` measurably faster than from a plain AL2023 AMI, with
    `/etc/lazycloud-node-image.json` present and the pinned versions matching
    `shared.tailscale_install`.
  - **Depends on**: BOOT-05

- [ ] **BOOT-09** Delete the tailnet-first rationale from surviving docstrings
  - **Files**: `packages/compute/src/compute/node_bootstrap.py:1-31`;
    `packages/gateway/src/gateway/pool_bootstrap.py:1-14`;
    `packages/compute/src/compute/providers.py:65-70`;
    `packages/networking/src/networking/tailnet_control.py:24-30`;
    `plan/00-prior-tailnet-plan.md`
  - **Change**: every module docstring in this territory explains why the node
    joins the tailnet before its first control-plane call. After BOOT-05 that is
    false. Rewrite them to describe the current design. Delete
    `plan/00-prior-tailnet-plan.md`, whose checklist and constraints describe a
    path that no longer exists — retain only the two constraints that survive
    (rebuild all source-bearing images together; `compute_provider_instances.payload
    ->>'bootstrap_failure_reason'` is the authoritative failure signal) by moving
    them into `deploy/AGENTS.md`.
  - **Risk**: none functional. Leaving them is worse than the risk: a false
    rationale in a docstring is what produced the `WantRunning` claim that cost a
    live debugging session (`plan/00-prior-tailnet-plan.md:17-23`).
  - **Acceptance**: `rg -n "Funnel|pool bootstrap key|bootstrap-<|tailnet before"`
    across `packages/` and `deploy/` returns nothing describing the deleted design.
  - **Depends on**: BOOT-07

## Deletions

| File or symbol | Lines | Why safe to delete | What replaces it |
| --- | --- | --- | --- |
| `pool_bootstrap.PoolBootstrapCredentialService` | `pool_bootstrap.py:45-143` | Its only consumer is `PoolBootstrapProvisioner.bootstrap`, which stops needing a key | Nothing — the node has no pre-enrolment tailnet identity |
| `PoolBootstrapCredentialRepository` | `repositories/compute.py:1152-1211` | Sole owner of the deleted table | — |
| `PoolBootstrapCredentialTable` + `compute_pool_bootstrap_credentials` | `tables/compute.py:402-418` + Alembic baseline | Predeployment; baseline is rewritten, not migrated | — |
| `PoolBootstrapCredential` | `compute_enrollment.py:88-111` | Contract for the deleted row | — |
| `TailscaleTailnetControl.issue_pool_bootstrap_key` | `tailnet_control.py:373-408` | Only caller is the deleted credential service | `issue_auth_key` (`:348-371`), single-use, 300 s |
| `pool_bootstrap_tag` / `_key_ttl_seconds` / `_key_refresh_seconds` / `DEFAULT_POOL_BOOTSTRAP_*` / `issuable_tags` / `bootstrap_tag_must_be_distinct` | `tailnet_control.py:26-30, 183, 189-196, 235-247`; `settings.py:72, 79-88` | Configure only the deleted key | Two tags, `validated_tags` returns two |
| `tailnet_auth_key` field ×3 | `providers.py:65-70`; `managed_pool.py:81-84`; `node_bootstrap.py:133` | No key is carried in user-data | Machine key from `/gateway/agents/transport-credential` |
| `ProviderPoolBootstrapFactory.release`, `_release_pool_bootstrap`, `writes_launch_template` | `service.py:172-189, 1560, 1640, 2395, 2558, 3729-3750, 3753-3780` | Both exist only to manage the pool key lifetime | — |
| Shell: `report`, `report_phase`, `report_failure`, `ts`, `parse_control_plane_origin`, `ensure_tailnet_service`, `is_tailnet_address`, `tailnet_join`, `CURL_RESOLVE` | `node_bootstrap.py:231-277, 357-453` | The agent reports and joins in Python from BOOT-03/BOOT-05 | `daemon.py:360-366` reports; `daemon.py:1251-1296` joins |
| Shell install steps: `docker_ready`, `ensure_docker`, `tailscale_ready`, `ensure_tailscale`, `agent_ready`, `ensure_agent` | `node_bootstrap.py:279-293, 295-350` | Re-implementations of the published provisioner's own steps | `operations.py:490-525` (Docker), `:571-614` (Tailscale, digest-pinned per arch), `:616-634` (agent) |
| Post-handoff watch loop | `node_bootstrap.py:508-517` | A strictly worse duplicate: it polls for `agent-state.json` and reads only `systemctl show -p Result`. Added today in `0743071`; superseded before it ever shipped to the target design | `wait_for_agent_service` (`operations.py:723-761`) — polls the runtime-ready marker *and* unit state, then emits `systemctl show` plus 80 redacted `journalctl` lines |
| Tailnet unit/socket/state constants | `node_bootstrap.py:57-66, 72-73` | No unit is written by user-data | Agent Managed mode owns its daemon |
| `tailscale_version` / `tailscale_sha256` on `NodeBootstrapSettings` | `node_bootstrap.py:134-135` | The provisioner pins the same version and per-arch digest itself | `operations.py:557-562`, `shared.tailscale_install` |
| Bash SigV4 (`sha256_hex`, `hmac_hex`, `uri_encode`, `mint_proof`, `report_identity_fields`, credential vars) | `managed_pool.py:1141-1149, 1158-1213, 1239-1242` | The only pre-agent proof consumer is deleted | `provider_node_proof.py:161-180` (botocore) |
| `--ssh` on the bootstrap join | `node_bootstrap.py:417-421` | Inert — `deploy/tailnet/policy.json.tftpl` has no `ssh` block, so Tailscale SSH denies by default | Agent-side phase and failure reports (BOOT-03) |
| `bootstrap_tag` across `deploy/tailnet/` | `policy.json.tftpl:5, 18-22, 30-33, 42-44, 46-56`; `variables.tf:56-65`; `main.tf:5, 23-24, 36`; `outputs.tf:23`; `terraform.tfvars.example:9` | No device ever carries it | Two tags, two grants |
| `_BAKE_USER_DATA_TEMPLATE` | `deploy/ami/bake.py:614-660` | Third copy of the install steps | `/install/agent` (`operations.py:203-805`) |
| Deleted-behaviour rows in bootstrap script tests | `packages/compute/tests/test_node_bootstrap.py`; `packages/providers/aws/tests/test_provider_aws_bootstrap_script.py` | Assert shell that no longer exists; fail Test Decision Gate items 1 and 3 | Assembler-contract assertions retained |
| `plan/00-prior-tailnet-plan.md` | whole file | Describes the superseded design | Two surviving constraints moved to `deploy/AGENTS.md` |

**Explicitly not deleted**: `provider_node_identity.py` and `provider_node_proof.py`
in full; `MetadataOptions` with `HttpTokens: required` and
`HttpPutResponseHopLimit: 1` (`managed_pool.py:1121-1126`); `TailnetCleanupCoordinator`
and its tombstones; `internal_http.py`; the phase/deadline model; the
compensating rollback in `ProviderNodeEnrollmentService.enroll`
(`provider_enrollment.py:106-116`); the route proxy's HMAC preface; the
`node_bootstrap_script` assembler contract (`:539-582`); `TailnetRuntimeMode.Sidecar`
(the control plane uses it).

## Track acceptance

1. `python -m tests.e2e.external.aws.one_machine_readiness` — exit 0, `ready: 1`.
   Observe in the tailnet device list that the node appears exactly **once**, as
   `lazycloud-agent-<machine_id>-g1`, and that no device ever carries the
   bootstrap tag. This is the single scenario that proves the whole reordering.
2. A deliberately broken boot reports its reason. Break the agent artifact URL for
   one launch and confirm
   `compute_provider_instances.payload->>'bootstrap_failure_reason'` names
   `agent_download_failed`, not `bootstrap_timed_out`. Repeat with Docker
   unavailable for `runtime_install_failed`. These two are the reasons that are
   structurally unreportable today.
3. A reported failure is reclaimed and replaced. From (2), observe the instance
   reach `Terminating` within one phase deadline and a replacement launch.
4. A revoked agent stops. Revoke a live machine and confirm the unit reaches
   `failed` and stays there, with no further join attempts in the control-plane
   access log.
5. `curl -fsS <public origin>/health` succeeds from an EC2 instance in the test
   customer account holding no tailnet session — the property BOOT-04 depends on,
   verified independently rather than inferred from a successful boot.
6. `python -m tests.e2e.external.aws.paid_resource_bounding`, then
   `python -m tests.e2e.external.aws.cleanup`. Cleanup must leave zero
   LazyCloud-owned tailnet devices, keys, launch templates, ASGs or instances,
   and must not touch unrelated tailnet state.
7. `terraform -chdir=deploy/tailnet plan` clean after BOOT-07, with the OAuth
   client replacement acknowledged and credentials re-exported.
8. `ruff`, `basedpyright`, and the changed-owner pytest scopes
   (`packages/compute`, `packages/networking`, `packages/gateway`,
   `packages/agent`, `apps/agent`, `packages/providers/aws`) green. Run with
   `-x` and a short timeout per owner, not one broad run.

## Open questions for the owner

1. **STS amplification on a public `/gateway/provider-nodes/*`.** Every enrol,
   phase and failure request makes a synchronous outbound STS `GetCallerIdentity`
   call with a 3 s timeout inside an API worker
   (`provider_node_identity.py:298-308`, `:29`). Unauthenticated and
   internet-facing, that is a request-amplification and worker-exhaustion
   primitive: N cheap POSTs occupy N workers for up to 3 s each. Options: (a)
   rate-limit by `enrollment_request_id` and source address at the edge before
   the handler; (b) cheap-reject before the STS call — the proof's expiry,
   credential scope and region are all verifiable locally
   (`provider_node_identity.py:382-510`) and only a proof that passes every local
   check should reach STS; (c) bound concurrent STS verification with a semaphore
   so saturation degrades enrolment rather than the whole API. I recommend (b)
   plus (c), with (a) as the INFRA-owned layer. **This must be decided before
   BOOT-04, and it may belong to 13-production-readiness rather than to me — tell
   me which.**
2. **`--login-server` / self-hosted coordination server.** Today
   `node_bootstrap.py:417-422` passes no `--login-server`, so a managed node always
   joins the public Tailscale coordination server even when
   `LAZYCLOUD_TAILNET_CONTROL_URL` points elsewhere — unlike the control-plane
   sidecar (`compose.yaml:562`) and the agent (`tailnet.py:714-715`). BOOT-05
   closes this by construction: the agent authenticates with the `control_url`
   the control plane returns (`daemon.py:1268-1272`). But
   `TailscaleTailnetControl` is bound to Tailscale SaaS `/api/v2`
   (`tailnet_control.py:22, 362-368`), so if a self-hosted coordination server is
   on the roadmap, key and device management is a separate rewrite that this track
   does not cover. **Is it on the roadmap?** If yes, BOOT-06 should not further
   entrench the SaaS API shape.
3. **Agent binary transfer moves onto the control plane unless `--agent-url` is
   added.** `install_agent` fetches from `$GATEWAY/install/agent/$OS_NAME/$ARCH_NAME`
   (`operations.py:628-630`), served as a `FileResponse` by the control plane
   (`apps/api/src/api/server/routers/install.py:43-75`). The artifact is
   **47.6 MB** (`dist/agent-binarys/*/manifest.json`, `size_bytes`). Today a
   managed node fetches it from a public release bucket instead
   (`node_bootstrap.py:343`), and `deploy/ami/bake.py:244-336` already publishes
   and verifies that anonymously-readable object. Adopting `/install/agent`
   without an artifact-source argument would put a 47.6 MB transfer per node
   launch onto the control-plane ingress — the same class of load that broke the
   previous public ingress. BOOT-05 therefore adds `--agent-url`. **Confirm you
   want the binary served from the release bucket rather than the control plane;
   if you would rather the control plane serve it, drop that step and size the
   ingress for it.**
4. **Inspection versus reclaim.** BOOT-01 terminates a node that reported a
   failure. That deletes the only remaining inspection window. I judge it correct
   because inspection is already impossible (no console output, no SSM, read-only
   credentials), but it is a deliberate reversal of the current comment at
   `node_bootstrap.py:217` and belongs to the owner. An alternative is a bounded
   hold — reclaim after 300 s rather than immediately — which BOOT-01 implements;
   confirm 300 s is the right number.
5. **Public exposure of the whole control-plane API.** BOOT-04 makes the origin a
   node dials the public one. The internal port already serves the entire API
   (`compose.yaml:640`), so this is a posture decision about what the public edge
   terminates and what it forwards, not just about enrolment. That is an INFRA
   decision I am consuming, not making.
