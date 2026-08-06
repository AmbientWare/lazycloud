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

- [ ] **Decide how the control plane scales.** Still open, but one of the four
      blockers is gone: the tailnet gateway is no longer pinned 1:1, and each
      replica would now get its own tailnet egress. What remains, all confirmed
      by reading rather than assumed:

      1. **One stable inbound name.** N replicas means N tailnet devices, and
         workers need a single address — a tailnet-facing ingress (Caddy or
         similar) holding `lazycloud-control-plane` and fanning out.
      2. **TCP ingress** binds `:1995` inside the API process
         (`apps/api/src/api/fastapi_app.py`, `tcp_ingress.start()`) with certs
         from the one-shot `tcp-certificate` service. It has to move out.
      3. **`_reconcile_agent_routes`** runs unleased in every API process; every
         other background loop is already replica-safe, and
         `AwsAccountConnectionService.reconcile_due` shows the pattern
         (`claim_due(now, lease_until, limit)`).

      Not blockers, confirmed: the agent channel is stateless polling, not a
      persistent stream (`apps/agent/src/agent_app/daemon.py` sleeps
      `stream_interval_seconds` around a plain POST); backend routes are durable
      in Postgres; the recovery fence takes `pg_advisory_lock_shared`, which is
      shared by design.

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
