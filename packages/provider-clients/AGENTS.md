# Provider Client Composition

Map persisted provider configuration and process settings to adapter instances.
This package does not own provider workflows, billing, capacity reconciliation,
API/CLI behavior, persistence tables, or process entrypoints. The production
registry accepts only live-evidence-supported AWS; self-hosted machines use agent
enrollment, and unsupported persisted kinds fail explicitly. Keep exports
explicit and preserve credential/construction failure behavior.
