# Provider Adapters

Provider packages are thin external adapters with `provider-*` distributions,
`provider_*` imports, and sources under `packages/providers/<name>`. AWS is the
only accepted machine provider. A new provider requires disposable live create,
agent registration, workload, deletion, and leak-audit acceptance; mocks are not
a support gate.

Kubernetes is deployment infrastructure and may scale only the exact Helm-owned
container-worker Deployment. Provider-neutral decisions remain in domain
packages; adapters never import apps, SDK, process composition, database
sessions, or workflow services and never expose credentials. External lifecycle
changes require disposable provider acceptance and cleanup.
