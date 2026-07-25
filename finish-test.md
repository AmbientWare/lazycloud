# Finish: live AWS certification (T-051 Tier 3)

## State

Tier 2 is **accepted**. The data-plane loop passes twice in a row on the Linux
ml-machine, proving request → scheduler → agent-managed worker → buildah image
build → archive upload → Function deploy/invoke → cleanup, with no AWS:

```sh
uv run python -m tests.e2e.local.function.scenario_invoke --live
# {"app": "...", "capability": "function.invoke", "result": 49}
```

Tier 3 — one paid AWS certification — is the only thing left. **AWS is at
verified zero right now** (0 instances, 0 ASGs, 0 volumes), so nothing is
costing money while this waits.

Repo: `~/programs/ambient/lazycloud` on ml-machine, `main`.

## The blocker: no public ingress

An EC2 machine phones home to the control plane over the Tailscale Funnel. The
Funnel is not serving, so a paid run would launch a machine that can never
report in. **Fix this first; do not start a paid stage until the Funnel answers
publicly.**

The gateway container (`tailnet-gateway`, which shares the control plane's
network namespace via `network_mode: service:control-plane`) is registered as
`lazycloud-control-plane-2.tailce6a2.ts.net` with Funnel configured, but stays
offline:

```
tailscale status  -> offline; "Tailscale cannot connect because the network is down"
tailscale netcheck -> UDP: false | IPv4: (no addr found) | Nearest DERP: unknown
```

DERP is Tailscale's relay fleet; a node that cannot reach any relay never comes
online. Measured from inside the control-plane namespace:

| check | from control-plane namespace |
|---|---|
| DNS `derp9.tailscale.com` | OK (`207.148.3.137`) |
| TCP 443 to DERP and to controlplane | OK |
| UDP STUN to DERP:3478 | **FAIL — TimeoutError** |

Measured on the host, both address families:

```
host IPv4 UDP: FAIL TimeoutError
host IPv6 UDP: FAIL TimeoutError
```

So **UDP egress to Tailscale is blocked outright on this LAN**, for IPv4 and
IPv6 alike. DNS is healthy and not intercepted (`192.200.0.10x` is Tailscale's
real IPv4 control-plane range; the local resolver simply prefers IPv6).

Tailscale falls back to relaying over TCP 443, and that path does open — but it
will not stay up. The gateway log flaps on a one-minute cycle:

```
21:59:44 health(warnable=no-derp-connection): ok
21:59:54 health(warnable=no-derp-connection): error: could not connect to the 'Denver' relay
22:00:44 ok
22:00:54 error
```

It reaches the relay, then loses it about ten seconds later, forever. The node
never stabilises, so it stays offline and the Funnel never serves. This is a
network problem on the ml-machine LAN (wireless `wlo1`), not a repo problem:
UDP blocked, and long-lived TCP to the relay being torn down.

Two measurement traps to avoid repeating here:

- `nc -zu host 3478` reports success for UDP without waiting for a reply. It
  gave a false "OK" that sent this investigation the wrong way. Send a real STUN
  packet and require a response, as above.
- BusyBox `nc -z` inside the gateway container is not GNU `nc` and reports
  failure spuriously. The gateway shares the control plane's network namespace
  (`network_mode: service:control-plane`), so measure from the control-plane
  container, whose tooling is trustworthy.

Options, in rough order of preference:

1. Put ml-machine on wired ethernet instead of wireless, and retest. Cheapest
   thing to try, and the flap pattern is consistent with a flaky link or an
   access point dropping long-lived connections.
2. Unblock UDP egress to Tailscale at the router/firewall — this is what
   production machines will rely on, so it is worth knowing either way.
3. Run the certification control plane on a host with working ingress. Note the
   laptop is a poor choice: it sleeps, and machines phone home for minutes.

## What is already prepared

- **Worker image published from the current commit**, so the managed-package
  digest skew that blocked the previous attempt cannot recur:
  `public.ecr.aws/y7n4m3x7/lazycloud/container-worker:74e447b`
  digest `sha256:b44f2fa290b6b2d80a6ead5d489f0110e3f5a560c5a6a14cdc01d58f0612f3d3`
  Build the control plane from the **same commit** — the digest is computed from
  managed package source, not image architecture, so an arm64 control plane and
  an amd64 worker match as long as the source does.
- `.env` on ml-machine is **local-shaped** (object storage on garage, gateway on
  the host IP, `LAZYCLOUD_AWS_CONNECTION_ENABLED=false`). The AWS-shaped values
  are preserved beside it as `.env.aws-configured-backup`.

## Steps

1. **Restore the AWS profile**: `cp .env.aws-configured-backup .env`, then set
   `LAZYCLOUD_AWS_CAPACITY_WORKER_IMAGE_DIGEST` to the digest above.
2. **Fix ingress** (above) and bring the stack up with credentials:
   `uv run python deploy/compose/activation.py`.
3. **Point the gateway URL at the real Funnel hostname.** The old value is
   `lazycloud-control-plane.tailce6a2.ts.net`, whose device no longer exists; the
   current gateway registers as `lazycloud-control-plane-2`. Read the actual name
   from `tailscale funnel status` in the gateway container and set both
   `LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL` and `LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL`
   to it, then recreate control-plane and scheduler.
4. **Prove the Funnel is publicly reachable — do not skip this.** A host on the
   tailnet resolves the Funnel name internally via MagicDNS and will appear to
   work while the public path is dead. This cost hours once already:
   ```sh
   IP=$(dig +short lazycloud-control-plane-2.tailce6a2.ts.net @8.8.8.8 | tail -1)
   curl -s -o /dev/null -w "%{http_code}\n" \
     --resolve "lazycloud-control-plane-2.tailce6a2.ts.net:443:$IP" \
     https://lazycloud-control-plane-2.tailce6a2.ts.net/health
   ```
5. **Run the four stages**, in order, stopping on the first failure. Commands and
   required arguments are in `tests/e2e/README.md` and `board/loop.md`.
   Expect: connection Ready → one machine Ready → Function returns its marker →
   cleanup to zero instances, volumes, and cost.
6. **End at verified zero.** Confirm 0 instances, 0 ASGs, 0 volumes and delete
   any `compute-connection-*-g*` stack the run created. Preserve every platform
   stack (`lazycloud-default-test-operator`, `lazycloud-connected-aws-compose`,
   `lazycloud-release-assets`, `lazycloud-shared`) and all unrelated resources.

## Traps already paid for — do not rediscover

- **MagicDNS makes a dead Funnel look alive.** Always verify with `--resolve`
  against the public IP from a resolver outside the tailnet.
- **`container-worker` is profile-gated.** `docker compose up --build` never
  builds it; the only symptom is the agent restart-looping on "Unable to find
  image". Build it explicitly with
  `docker compose --profile owner-direct-worker build container-worker`.
- **A lingering `docker compose` process does not mean work is happening.** Two
  builds this session sat at 0% CPU while the process existed. Judge progress by
  log mtime and the log's last line, not by `pgrep`.
- **`pgrep -f <pattern>` matches your own shell command**, so a finished run can
  look like it is still going.
- Failed transfers and daemon startups now name their cause; if you see a bare
  "transient errors" message with no host, you are on an old worker image.
- An agent whose state directory is reset while its worker container still
  exists fails forever with a Docker name conflict. Remove the stale
  `lazycloud-agent-<uuid>` container by hand. This is an unfixed robustness gap.

## Known-open, unrelated to this run

- `apps/agent` `test_artifact_settings.py::test_agent_artifact_settings_require_atomic_immutable_configuration`
  expects "configured together"; validation says "agent artifact binary directory is required".
- `packages/gateway/tests/test_gateway_api_auth_streaming.py` — two pre-existing failures.
- Image build staleness is only checked when **claiming**; a client already
  attached to a build follows it forever if that build's driver dies.
