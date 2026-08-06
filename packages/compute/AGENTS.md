# Compute Package

Provider-neutral capacity: offers, units, machines, private-agent state, managed
capacity lifecycle, billing hooks, and the protocols providers implement.

Provider adapters, scheduler and worker loops, the gateway, apps, and the SDK
stay outside; notify them through narrow hooks rather than importing them.
Durable access goes through services and repositories, hot state through
coordination primitives.

- Capacity authority belongs here. A caller asks for capacity; it does not decide
  what exists.
- Preserve retries, billing attribution, and resource cleanup on every path that
  can create something billable. A leaked machine is a bug with a running meter.
- A credential never travels in machine configuration that provisioning can read
  back. Prefer a single-use credential vended at the moment of use over a
  reusable one embedded where every instance from that configuration can read it.
- A bootstrap script owns provider identity and nothing else. Everything it
  installs belongs to the artifact it downloads, so a version or a policy is
  pinned in one place instead of drifting between two.
- Distinguish a nonce from an authenticator. A value that grants nothing without a
  second, separately verified identity may be stored in the clear; a value that
  authenticates goes through the workspace secret cipher. Know what that cipher
  actually protects against before relying on it, and say so where it is used.
