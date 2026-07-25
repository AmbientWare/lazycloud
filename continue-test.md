# Continue: local data-plane loop on a Linux host

## What this is

The Tier 2 loop from T-051: prove the **data plane** — request → scheduler → agent-managed
worker → image build executes → Function returns its exact result → clean teardown — against the
real Compose stack, with no AWS involved.

This replaces using paid EC2 machines to debug provider-neutral behaviour. Every defect found in
the last two sessions was provider-neutral; each cost a paid machine and an SSM session to find.

## Why it must run on Linux

The agent starts worker slots with `--network host`. Under Docker Desktop for Mac that namespace
has an address but **no IPv4 default route**, so `CommandNetworkSystem.discover_host_capabilities`
raises "worker host has no IPv4 default-route interface"
(`packages/worker/src/worker/network_backend.py:321`) and the worker crash-loops.

A colima VM clears that specific blocker, but introduces its own problems (below). A real Linux
host has none of them.

## Setup on the ml-machine

```sh
# 1. repo + .env (the .env is required: bootstrap token, tailnet credentials)
cd <repo>

# 2. bring the whole stack up. tailnet is unconditional now — the tailnet-gateway
#    sidecar is part of the stack and no longer behind a Compose profile.
docker compose up -d

# 3. wait for health, then log in against the fresh database
docker compose ps
uv run lazycloud login --endpoint http://127.0.0.1:8000 --token "$(grep '^LAZYCLOUD_TOKEN=' .env | cut -d= -f2- | tr -d "'\"")"

# 4. run the loop
uv run python -m tests.e2e.local.function.scenario_invoke --live
```

### Disk requirement (this is a hard gate)

The image build refuses to start unless the worker's build filesystem has
`per_build_max_bytes (16 GiB) + minimum_free_bytes (5 GiB) = 21 GiB` free. The path is
`LAZYCLOUD_COMPOSE_AGENT_STATE_DIR` (default `/var/lib/lazycloud/agent`).

That path must satisfy **both** of these:

- It is where the agent keeps `cache/`, `builds/`, and `images/`, so it needs ≥21 GiB free.
- The agent creates worker slots **through the Docker socket**, passing this path as the bind
  source. The Docker daemon resolves it, so the path must mean the same thing to the agent
  process and to the daemon. Do not put it on a bind mount from another machine, and do not
  back it with a named volume — the daemon will resolve the literal path instead.

## What success looks like

The scenario deploys a Function, which forces an image build onto the agent-managed worker,
invokes it, checks the exact return value, and deletes the app. Green means the whole data plane
works. Run it twice — the second run proves it is repeatable and self-cleaning.

Intermediate checks, in the order they must become true:

```sh
# machine registered and eligible
docker compose exec redis redis-cli --scan --pattern "*pools:default:machines:*"
# -> record should show preflight_passed: true, schedulable: true

# worker visible to the scheduler
docker compose exec redis redis-cli smembers "lazycloud:scheduler:workers:index"

# source cache activated (this is what gates request pickup)
docker compose exec postgres psql -U lazycloud -d lazycloud -c \
  "select worker_id, state from worker_cache_generations order by updated_at desc limit 3;"
# -> state must reach 'available'

# build progress
docker compose exec postgres psql -U lazycloud -d lazycloud -c \
  "select status, right(payload->>'logs',300) from image_builds order by created_at desc limit 1;"
```

## Where the last run stopped

On colima the loop reached the **final hop**: the build container was created on the worker, and
the control plane's dial to it timed out.

```
FunctionOperationError: backend route <machine>:<worker>:build-<id>:worker:0 dial timed out
```

Working hypothesis: this is the Mac VM, not the code. Two containers inside one VM behind the same
NAT need a working tailnet mesh to reach each other; the control-plane device reported `offline`
in its own `tailscale status`, and that host's Tailscale could not reach the coordination server
all session. On a single Linux host this path is far simpler. **Confirm before assuming** — if the
dial still times out on real Linux, it is a genuine defect in backend route dialling.

## Environment traps already paid for — do not re-derive

- **Mac disk**: builds landed on the host filesystem through virtiofs, which was 99% full. Freed
  46 GB of Docker build cache. Check `df -h` before blaming the code.
- **virtiofs cannot host unix sockets**: tailscaled failed with
  `safesocket.Listen: bind: operation not supported` when its state dir was a bind-mounted macOS
  directory. Irrelevant on a real Linux host.
- **Stale agent identity**: a state dir left over from a previous stack causes
  `tailnet enrollment is not awaiting this device`. Clear the agent state dir when pointing at a
  fresh database.
- **Stale worker container**: if agent state is reset while its worker container still exists, the
  agent fails forever with a Docker name conflict and cannot reconcile it. Remove the
  `lazycloud-agent-<uuid>` container by hand. This is a real robustness gap worth fixing.
- **`capacity.max_memory` preflight**: the agent requests `--max-memory 8192MiB`; a host detecting
  less marks the machine unschedulable with "requested memory exceeds detected memory", and the
  only visible symptom is `retry-limit: no schedulable workers`.

## Fixed this session (already committed)

- image builds are placed by the workspace policy — the Compose pool-selector default caused
  every AWS `retry-limit`
- transport failures are recoverable; agent startup retries instead of exiting
- the agent systemd unit has no start rate limit and one owner (`render_systemd_unit`)
- tailscaled's output is captured; a failed start now explains itself
- the tailnet is unconditional — no enable flag, no profile gate, and agent enrollment always
  requires a verified bound tailnet device
- the scheduler reports why a request could not be placed, and that reaches the SDK
- the container dispatch diagnostic route works instead of returning 500

## Still open

- `apps/agent` has a pre-existing unrelated failure:
  `test_artifact_settings.py::test_agent_artifact_settings_require_atomic_immutable_configuration`
  expects "configured together" but validation says "agent artifact binary directory is required".
- `packages/gateway/tests/test_gateway_api_auth_streaming.py` has two pre-existing failures
  (confirmed failing with this session's changes stashed).
- Image build staleness is only checked when **claiming** a build. A client already attached to a
  build follows it indefinitely if that build's driver dies.
- AWS certification (T-051 Tier 3) is unrun and should stay unrun until this loop is green.
