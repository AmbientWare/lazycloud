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

- [ ] **Reconcile `.env.example` with `.env`.** 24 keys are set locally and
      documented nowhere, including `LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL` — the
      exact key behind the trap above, which a new developer copying
      `.env.example` never sees. Secrets (`LAZYCLOUD_TAILNET_OAUTH_CLIENT_SECRET`,
      `LAZYCLOUD_BACKEND_ROUTE_AUTH_KEY`) go in as empty placeholders, never with
      values. `LAZYCLOUD_TAILNET_AUTH_KEY` is no longer read by anything and
      should come out of `.env` rather than into `.env.example`. Decide separately whether
      the `LAZYCLOUD_E2E_*` and `AWS_PROFILE` entries belong there at all or are
      local-only.

- [ ] **GPU AMI location — decision needed, not obviously work.** Flagged
      earlier as "should move to the release manifest", but
      `AwsCapacityEnvironmentSettings`
      (`packages/provider-clients/src/provider_clients/settings.py:125`)
      deliberately keeps it out: which AMIs carry GPU drivers is the deployment's
      decision, while the worker image, agent URL, and CPU AMIs are facts of the
      release. `deploy/release.py` accepts only `--cpu-ami`. Moving it means
      revisiting that decision, so decide before doing.

## Release

- [ ] **Publish a new agent artifact.** The shipped agent still carries the 2s
      route-proxy timeout fixed in `bf6fd292`: `dial_local_target` passed its dial
      budget to `socket.create_connection`, which leaves the timeout on the socket
      for its whole life, so any proxied route quiet for two seconds was torn
      down mid-session. Connected-AWS nodes have broken terminals and streaming
      until a new artifact ships. Deliberately last, so it is built after the work
      above in case any of it touches the agent.
