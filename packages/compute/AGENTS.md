# Compute Package

Own provider-neutral offers, pools, machines, private-agent state, managed
capacity lifecycle, billing hooks, and provider protocols. Provider adapters,
scheduler/worker loops, gateway, apps, and SDK remain outside; notify them
through narrow hooks. Durable access stays in services/repositories and Redis
state behind coordination. Preserve capacity authority, retries, billing, and
resource cleanup through the affected real owner path.

A node's user-data carries no credential. It reports to the public origin,
because a machine in a customer VPC holds no tailnet session when it first
reports, and it joins the tailnet with the single-use machine key enrolment
vends it. That is why there is no pool-scoped tailnet key, no table behind one,
and no third tailnet tag: a reusable key in a launch template is a credential
sitting in plaintext on every instance that template ever starts, and it bought
only the ability to report a few phases earlier.

The bootstrap script owns provider identity and nothing else. Docker, Tailscale,
the agent binary, and the agent's systemd unit belong to the published installer
it downloads and runs. When this script carried its own copies they drifted from
the installer's — a unit written in two places with two restart policies, and a
Tailscale version pinned here against one pinned per-architecture there.

The AWS connection `external_id` is stored in the clear, deliberately. It is a
confused-deputy nonce, not an authenticator: the customer enters it as a
CloudFormation parameter, the platform validates that their trust policy
enforces it, and holding it grants nothing without also being the trusted
platform principal. Encrypting a value the customer types into their own
console would buy nothing.

Secrets that *are* authenticators go through `WorkspaceSecretCipher`. Note its
honest limit before relying on it: its root is `workspaces.signing_key`, itself
a plaintext column, so it provides domain-separated encryption at rest within
one database — not envelope encryption against a compromised dump.
