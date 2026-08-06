# The control plane runs its own tailnet device

Resolved. This is kept as the record of a failure that was expensive to diagnose
three times, and of why the fix is shaped the way it is.

## What used to happen

`tailnet-gateway` and `public-ingress` both ran with
`network_mode: "service:control-plane"`, so neither had a network namespace of
its own — they borrowed the control plane's. Compose resolves `depends_on` in one
direction only: recreating a dependent starts its dependency first, but
recreating a dependency does **not** recreate its dependents. So

```sh
docker compose up -d --build control-plane
```

replaced the control-plane container, and its namespace, while both sidecars kept
running attached to a namespace that no longer existed.

Rebuilding the control plane is the most common action in development, so this
fired constantly. It was silent at every layer that could have reported it:

- Both sidecars stayed `running (healthy)`. The tailnet healthcheck asked
  `tailscale status` whether its own session was up, which it was — that says
  nothing about whether anything is listening behind it.
- A worker container got `WORKER_REPOSITORY_URL` naming a tailnet host that no
  longer resolved, failed DNS, never registered, and sat at `pending` forever.
  It wrote no logs, the agent wrote no logs, and the control plane logged healthy
  agent streams throughout. The visible symptom was a scheduler with a worker
  record but no available worker, which reads exactly like a scheduling bug.
- One connected-AWS acceptance run lost every EC2 node to
  `curl: (22) The requested URL returned error: 530` — Cloudflare reporting no
  reachable origin, because the tunnel had been pointed at a namespace destroyed
  hours earlier. `https://lazycloud.dev` served nothing and no node could fetch
  the agent binary.

The workaround became institutional: `deploy/release.py` carried a function whose
docstring read *"Restart, and put the sidecars back in the namespace they lost."*

## What replaced it

The control plane runs its own `tailscaled` (`TailnetRuntimeMode.Managed`) rather
than borrowing a sidecar's namespace. `tailnet-gateway` is deleted,
`public-ingress` reaches the origin over the ordinary Compose network, and
`network_mode:` no longer appears in `compose.yaml` at all. There is no shared
namespace left to orphan, so the failure is structurally impossible rather than
guarded against.

This is not a new mechanism. `Managed` mode is what every agent has always used,
including every EC2 node in the connected-AWS runs, and the control-plane image
already shipped both `tailscale` and `tailscaled`. The control plane was the only
component still on the exceptional path; that path is now deleted.

Its device identity persists in the `control-plane-tailnet-state` volume, so a
restart resumes rather than re-registers. Because `tailscale up` advertises no
tag of its own, the tag can only arrive on the key that redeems it — so the
control plane mints its own tagged, short-lived key from the OAuth client it
already uses for agents. No deployment holds a long-lived tailnet auth key, and
an untagged control-plane device is no longer possible.

## The second trap, which was independent

Chasing the above turned up a different failure with an identical symptom, worth
knowing because the fix above does not address it.

`LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL` in a local `.env` named the control plane's
tailnet peer as `lazycloud-control-plane-1` — the Compose *container* name, with
its replica suffix. The tailnet device is registered under `TS_HOSTNAME`, which
has no `-1`. The name never resolved and the worker failed exactly as above:
pending forever, no logs anywhere. A Compose container name is not a tailnet
device name, and it is an easy substitution because the container name is what
`docker ps` shows.

The control plane's healthcheck now resolves the host it advertises to workers,
so this surfaces as an unhealthy container within seconds instead of an hour of
looking for a scheduling bug. It checks that the name resolves, not that it
points at this deployment — a name that resolves to the wrong host still passes.

## The lesson worth keeping

A healthy container is not a working path. Both sidecars reported healthy for
hours while serving a dead namespace, and the tailnet reported `Online: True`
throughout. Check the boundary end to end rather than trusting aggregate status.
