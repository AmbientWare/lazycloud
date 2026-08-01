# Compute Package

Own provider-neutral offers, pools, machines, private-agent state, managed
capacity lifecycle, billing hooks, and provider protocols. Provider adapters,
scheduler/worker loops, gateway, apps, and SDK remain outside; notify them
through narrow hooks. Durable access stays in services/repositories and Redis
state behind coordination. Preserve capacity authority, retries, billing, and
resource cleanup through the affected real owner path.

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
