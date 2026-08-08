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

A connected cloud account belongs to a user, not a workspace, and backs every
workspace that user owns. Runtime lookups therefore resolve
`workspace -> owner -> connection` through `get_for_workspace_owner`, and anything
that acts on the whole connection—draining its pools, reconciling its bucket
grants—gathers every workspace the owner holds first. Snapshotting one workspace
and applying it as the connection's whole state would revoke what the others rely
on.

Placement compares owners. A private worker is the customer's own machine and
serves every workspace that customer owns; accept the consequence deliberately,
that private capacity is no longer a hard isolation boundary between an owner's
own environments. It stops at the account. Platform-managed capacity is shared and
governed by a separate rule that no owner comparison may widen.
