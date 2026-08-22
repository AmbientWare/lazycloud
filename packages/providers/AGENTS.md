# Provider adapters

Thin adapters over external infrastructure providers: one distribution per
provider, sources under `packages/providers/<name>`, with `provider-*`
distribution names and `provider_*` imports.

An adapter translates provider-neutral requests into provider calls and back.
Provider-neutral decisions stay in the domain packages. Adapters never import
apps, SDK, process composition, database sessions, or workflow services, and
never expose credentials.

Supporting a provider is a claim about external behavior, so it is proved against
the real provider: disposable create, agent registration, a real workload,
deletion, and an audit that nothing was left running. A mock is not a support
gate. Changes to external lifecycle carry the same bar, cleanup included.
