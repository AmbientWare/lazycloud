# One tailnet daemon per node

## Why

A managed-pool node must reach the control plane over the tailnet, not a public
Funnel origin. The current implementation gets a node onto the tailnet and its
first two phase reports through — verified on three EC2 instances — and then
fails at the handoff to the agent.

The bootstrap script runs `tailscaled` as a child of cloud-init, at the *agent's*
state directory, and stops it before `install-service`. The teardown exists only
because `TailnetRuntime` in Managed mode spawns and owns its own daemon, and
`_managed_process_alive()` recognises only a process it started — so leaving the
first daemon running would produce a second one on the same state file and TUN
device.

That teardown is unrecoverable, not merely fragile. The pool bootstrap key is
`ephemeral=True`, so stopping the daemon makes Tailscale **delete the device**.
The agent's daemon then comes up with a revoked node key, reports `NeedsLogin`,
and enrolment proceeds over a tailnet the node is no longer on — hanging until
the bootstrap deadline. The claim in `node_bootstrap.py` that `--reset` persists
`WantRunning=true` so the agent resumes the same node key is false for an
ephemeral key.

So: `tailscaled` starts once as a systemd unit and stays up for the node's
lifetime. The bootstrap joins it and reports; the agent attaches to the same
daemon in sidecar mode. One owner, nothing to resurrect. Sidecar is already the
most proven path in the repo — the control plane runs that way today.

## Checklist

### Networking runtime

- [x] `tailnet.py` — `authenticate()` accepts sidecar mode, and
      `_start_managed_daemon()` is called only when the runtime owns its daemon.
      Both together: relaxing the guard alone makes rotation spawn a rival
      daemon against the live socket.
- [x] `tailnet.py` — `_verify_sidecar` raises `TailnetAuthenticationRequired`
      when the terminal state is `needslogin`, so an agent restarting into an
      unauthenticated sidecar self-heals the way Managed already does.

### Node bootstrap

- [x] `node_bootstrap.py` — unit name/path constants (`lazycloud-tailscaled.service`,
      never `tailscaled.service`, which upstream owns) and their sentinels.
- [x] `node_bootstrap.py` — `ensure_tailnet_service()` renders the unit,
      `daemon-reload`, `enable`, `restart`, then polls readiness.
      **`ExecStart` must be an absolute path** — `TAILSCALED_BINARY` is
      PATH-resolved and systemd rejects a relative `ExecStart`, which would fail
      every node.
- [x] `node_bootstrap.py` — `tailnet_join()` keeps the key file, `ts up`, peer
      pin and `getent` check; loses the daemon spawn and `$TAILSCALED_PID`.
- [x] `node_bootstrap.py` — delete `tailnet_stop()`; new `bootstrap_main`
      ordering with no teardown before `install-service`.
- [x] `node_bootstrap.py` — `install-service` gains
      `--tailnet-mode sidecar --tailnet-socket "$TAILNET_SOCKET"`. Do not pass
      `--tailnet-state-dir`: the agent already derives it from the same
      constants, and a second statement of one fact is a second thing to drift.
- [x] `node_bootstrap.py` — module docstring drops the false `WantRunning`
      claim and describes the single-owner model.

### Agent

- [x] `daemon.py` — delete `_attach_tailnet` and its plumbing in `run()`.
- [x] `daemon.py` — `_start_tailnet` back to its single-argument form.

### Evidence

- [x] `test_node_bootstrap.py` — drive the generated script through real `bash`
      with a fake `systemctl` that actually backgrounds the unit's `ExecStart`,
      and assert **the daemon is still alive when `install-service` runs**. This
      single assertion is the failure we hit.
- [x] `test_node_bootstrap.py` — assert the rendered `ExecStart` is absolute and
      its `--socket`/`--state` equal `TAILNET_SOCKET_PATH`/`TAILNET_STATE_FILE`.
- [x] `test_tailnet_runtime.py` — sidecar `authenticate(force=True)` issues
      `logout` then `up` on the same socket and spawns no daemon.
- [x] `ruff`, `basedpyright`, full `pytest` green.

### Live

- [x] Rebuild every source-bearing image together, from one source state.
- [x] AWS account connection — survived cleanup, phase `ready`.
- [x] A control plane addressed by its tailnet address no longer refuses the
      join. Pointing the origin at the CGNAT address made `tailnet_join` ask
      `tailscale ip` to resolve an address, so a node that was on the tailnet
      and reporting refused its own boot as `network_join_failed`.
- [x] An agent that dies after handoff now reports. Until this, cloud-init had
      exited and the unit was detached, so a crash and a node that never booted
      were the same observation: `bootstrap_timed_out`, naming nothing.
- [x] The published agent artifact accepts `--tailnet-mode sidecar` and
      `--tailnet-socket` — checked by running it, not by reading the branch.
- [ ] `one_machine_readiness` — node reaches ready, device rotates
      `bootstrap-<instance-id>` → `lazycloud-agent-<machine_id>-g1`.
- [ ] `paid_resource_bounding`, then `cleanup`.

Still unexplained: two nodes ended `bootstrap_timed_out` with no detail. They
predate both fixes above, so the next timeout should name itself.

## Constraints that bit already

- Rebuild **all** source-bearing images together or the managed runtime rejects
  the mismatch as a package digest error.
- Recreating `control-plane` kills `tailnet-gateway` (shared network namespace)
  and Compose does not restart it. Follow with `docker compose up -d tailnet-gateway`.
- Read the control plane's tailnet name from the running sidecar; do not assume,
  and do not delete a device to reclaim a nicer name.
- The assembler contract: `__SENTINEL__` + `shlex.quote`, never format strings;
  `bootstrap_main` stays the literal last line.
- `compute_provider_instances.payload->>'bootstrap_failure_reason'` is the
  authoritative signal for a node that failed. It named every failure this
  branch hit; the tailnet device list and the access log did not, and reading
  them instead produced two wrong diagnoses.
- A node whose bootstrap reports a terminal failure keeps running — the error
  path leaves it for inspection — and it holds the ASG's only slot, so no
  replacement launches. Drive the policy to zero workers to clear it; the
  reclaim path does not.
- The test credentials are read-only for autoscaling and denied
  `ec2:GetConsoleOutput` and `ec2:DescribeLaunchTemplates`, so a node cannot be
  inspected out-of-band. Verify the generated script against the running
  control plane instead of the launch template.
