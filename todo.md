# Open work

Tracked here rather than as tickets: each item is contained enough to go
straight to a pull request. Ordered — networking first, agent artifact last.

## Networking

- [x] **Stop a control-plane rebuild orphaning its sidecars.** Done, but not the
      way this entry originally proposed. Rather than adding a namespace holder
      to protect the shared namespace, the control plane now runs its own
      `tailscaled` and `network_mode:` is gone from `compose.yaml` entirely, so
      there is no shared namespace to orphan. `tailnet-gateway` is deleted and
      the workaround in `deploy/release.py` with it. See `network-update.md`.

- [x] **Make a broken tailnet path loud.** The control plane's healthcheck now
      resolves the runtime host it advertises to workers, so a name that points
      nowhere shows up as an unhealthy container instead of workers pending
      forever with no logs. A tailnet startup failure is also fatal now rather
      than a logged warning.

- [x] **Decide how the control plane scales.** Decided: **one front door, replicas
      behind it.** A tailnet-facing ingress owns the stable name; each replica
      keeps its own tailnet device for egress to agents.

      The deciding constraint is who holds the address and for how long. Our own
      processes can re-resolve per use, so a registry of live replicas would
      serve them. A container cannot: it is handed `GATEWAY_HTTP_URL` once at
      start and holds it for its whole life, so a long-lived pod given one
      replica's address is stranded when that replica dies. A front door is the
      only shape where nothing downstream holds a replica-specific address.

      It also costs no consumer change. The registry stays single-valued and
      every caller keeps reading one published origin, which is the property
      worth protecting — the same call everywhere, no divergence.

      What it implies, when replicas are actually wanted:

      1. **The publisher moves, the readers do not.** Today the control plane is
         its own front door and publishes the device it registered. With an
         ingress, the ingress becomes the front door and publishes instead.
         `resolve()` and every consumer are untouched.
      2. **Replicas still each need a tailnet device.** Egress to agents — shell
         tunnels, route dialing — is per replica and no proxy substitutes for
         it. Naming them individually is free now that nothing hardcodes a name.
      3. **TCP ingress does not have to move.** Routes resolve by SNI
         (`{stub_id}-{port}.{external_host}`) out of Redis, so any replica can
         already serve any TCP route. It needs an L4 entry point in front of N
         listeners on `:1995`, which the same front door can be.
      4. **`_reconcile_agent_routes` needs a lease**
         (`apps/api/src/api/fastapi_app.py`). It runs unguarded in every API
         process and would double-run;
         `AwsAccountConnectionService.reconcile_due` shows the pattern with
         `claim_due(now, lease_until, limit)`. Harmless at one instance, so not
         built yet.

      Confirmed replica-safe already, by reading rather than assumption: the
      agent channel is stateless polling, backend routes are durable in
      Postgres, AWS reconciliation leases its work, the recovery fence takes
      `pg_advisory_lock_shared`, and TCP route state is in Redis.

## Configuration

- [x] **Reconcile `.env.example` with what the code reads.** Rewritten from the
      Compose interpolations and the Pydantic settings classes rather than from
      `.env`, and every value it sets is now proven equal to Compose's own
      default by rendering the file as an env-file. The `LAZYCLOUD_E2E_*` and
      `LAZYCLOUD_AWS_CONTROL_STACK_NAME` entries stayed out: they are outputs of
      `deploy/connected-aws/bootstrap.py` for opt-in live scenarios, documented
      at `deploy/README.md` and `tests/e2e/README.md`, and nothing in the stack
      reads them. Bare `AWS_PROFILE` stayed out too — Compose never interpolates
      it, so it looks like it selects the containers' profile and does not;
      `LAZYCLOUD_COMPOSE_AWS_PROFILE` is documented in its place.

      Two premises in the original entry were wrong.
      `LAZYCLOUD_TAILNET_AUTH_KEY` is still read: it is a field on
      `TailnetRuntimeOptions`, and `TailnetRuntime._resolve_auth_key` prefers a
      configured key over the one it would mint. What is true is that Compose
      hands it to no service, and `tests/deployment/test_customer_compute_configuration.py`
      keeps it that way — so it does not belong in an env-file, but the field is
      not dead. Nothing needed removing from `.env.example` either: it documented
      25 keys and all 25 were still read.

- [x] **Give the agent binary directory two names.** Done: the host bind source
      is `LAZYCLOUD_COMPOSE_AGENT_BINARY_DIR`, matching the `LAZYCLOUD_COMPOSE_*`
      convention for host paths, and `LAZYCLOUD_AGENT_BINARY_DIR` keeps its one
      meaning as the in-container path. Original entry: `LAZYCLOUD_AGENT_BINARY_DIR`
      is both the host directory the control plane bind-mounts and the
      in-container path it then reads, with a different default for each
      (`compose.yaml`, the `x-agent-binary-env` anchor and the `control-plane`
      volume). Any single value satisfies at most one of them, so the variable
      only works while it is unset — the reason `.env.example` documents it as
      absent rather than as an override.

- [x] **GPU AMI location.** Decided: **a fact of the release, like the CPU AMIs.**
      The original entry's premise was that which AMIs carry GPU drivers is the
      deployment's choice. It is not, once the repository bakes them: a GPU AMI is
      only usable by the gVisor build that ships in the same release, because
      nvproxy proxies driver ABIs it was compiled against. Leaving the AMI to the
      deployment means a deployment can pair a driver with a sandbox that cannot
      talk to it, and the failure appears as GPU containers that will not start.

      So `deploy/release.py` takes `--gpu-ami` alongside `--cpu-ami`,
      `capacity_gpu_ami_ids` rides the release manifest, and
      `AwsCapacityEnvironmentSettings` no longer owns it — `gpu_ami_ids` now lives
      on `AwsCapacitySettings`, resolved from the manifest the same way the CPU
      AMIs are. `deploy/ami/bake.py --variant gpu` produces the image and refuses
      to register one whose installed driver the pinned gVisor does not proxy.

      Fixed on the way past: `AwsCapacitySettings.configured` required a non-empty
      GPU catalog, so a CPU-only deployment advertised no AWS regions at all.

## Release

- [ ] **Publish a new agent artifact.** The shipped agent still carries the 2s
      route-proxy timeout fixed in `bf6fd292`: `dial_local_target` passed its dial
      budget to `socket.create_connection`, which leaves the timeout on the socket
      for its whole life, so any proxied route quiet for two seconds was torn
      down mid-session. Connected-AWS nodes have broken terminals and streaming
      until a new artifact ships. Deliberately last, so it is built after the work
      above in case any of it touches the agent.
