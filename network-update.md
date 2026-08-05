# Compose tailnet sidecar loses its namespace when the control plane is rebuilt

## What happens

`tailnet-gateway` runs with `network_mode: service:control-plane` (`compose.yaml`),
so it does not have its own network namespace — it joins the control plane's.
The service that changes most often owns the namespace, and the stable sidecar
borrows it.

Docker Compose resolves `depends_on` in one direction only. Recreating
`tailnet-gateway` starts `control-plane` first, as expected. Recreating
`control-plane` does **not** recreate its dependents. So an ordinary

```
docker compose up -d --build control-plane
```

replaces the control-plane container — and its network namespace — while
`tailnet-gateway` keeps running, still attached to the namespace of a container
that no longer exists.

## How to recognise it

The sidecar reports `running (healthy)`, which is why this is easy to miss. The
tell is that its namespace peer is not the live control plane:

```
docker inspect lazycloud-tailnet-gateway-1 --format '{{.HostConfig.NetworkMode}}'
# container:caa0f9598296…        <- dead container

docker inspect lazycloud-control-plane-1 --format '{{.Id}}'
# 9dc71eeb4b96…                  <- live container
```

Downstream, nothing can resolve the control plane's tailnet peer name. A worker
container gets `WORKER_REPOSITORY_URL=http://lazycloud-control-plane-1.<tailnet>.ts.net:9000`,
fails DNS, never registers, and sits at `pending` forever. It writes no logs, the
agent writes no logs, and the control plane logs show only healthy agent streams.
The visible symptom is a scheduler that has a worker record but no available
worker, which looks like a scheduling bug and is not one.

`docker compose logs tailnet-gateway` may also show
`health(warnable=no-derp-connection)`, but that is a relay warning and appears in
healthy runs too — it is not the signal.

## Immediate remedy

```
docker compose up -d --force-recreate tailnet-gateway
```

Compose rejoins the sidecar to the current control-plane namespace (and, because
of `depends_on`, starts the control plane first if needed).

## Why it is worth fixing properly

It recurs. Rebuilding the control plane is the single most common action during
development, and every one of those rebuilds silently breaks tailnet resolution
until someone notices. The failure is silent at every layer that could report it,
so the cost is not the fix — it is the hour spent looking for a scheduling or
enrollment bug that does not exist. It has caught more than one person.

## How to fix it

**1. Give the namespace its own owner (recommended).** Add a minimal
do-nothing container whose only job is to hold the network namespace, and have
both `control-plane` and `tailnet-gateway` join it:

```yaml
  netns:
    image: alpine:3
    command: ["sleep", "infinity"]
    # ports the namespace must expose are published here

  control-plane:
    network_mode: "service:netns"

  tailnet-gateway:
    network_mode: "service:netns"
```

This is the Kubernetes pod model — the `pause` container exists for exactly this
reason. Either real service can then be rebuilt freely; the namespace outlives
both. Note that published ports move to the holder, since a container joining
another's namespace cannot declare its own.

**2. Invert the ownership (smaller, partial).** Make `control-plane` join
`network_mode: service:tailnet-gateway`. Protects the common case, because the
app is rebuilt constantly and the sidecar almost never. It only relocates the
hazard rather than removing it: recreating the sidecar would then strand the
control plane.

**3. Make it loud (do this regardless).** Give the control plane a healthcheck
that resolves its own tailnet peer name. This does not prevent the breakage, but
it turns a silent, hours-long misdiagnosis into a container that goes red at the
moment it happens. The silence is what makes this expensive.

Recommended: **1 plus 3**.

## A second, independent trap in the same area

Chasing the above turned up a different failure with an identical symptom, so
both are worth knowing before touching this again.

`LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL` in a local `.env` named the control plane's
tailnet peer as `lazycloud-control-plane-1` — the Compose *container* name, with
its `-1` replica suffix. The tailnet device is registered under `TS_HOSTNAME`,
which defaults to `lazycloud-control-plane` (`compose.yaml:430`), with no `-1`.
The name therefore never resolved, and the worker failed exactly as above:
pending forever, no logs anywhere.

The two names come from different places and nothing checks that they agree. A
Compose container name is not a tailnet device name, and it is an easy
substitution to make because the container name is what `docker ps` shows.

Worth doing when the fix above lands:

- derive the runtime URL from `TS_HOSTNAME` rather than restating it in `.env`,
  so the two cannot drift; or
- validate at control-plane startup that its own advertised runtime host
  resolves, and fail loudly if not.

The second is the same "make it loud" point as (3) below, and it would have
caught both failures at the moment they occurred rather than hours later.

## Scope

Contained to `compose.yaml`, plus wherever published ports are declared for the
control plane. No application code changes. Verify by rebuilding the control
plane alone and confirming a worker container still resolves the control plane's
tailnet name and reaches `available`.
