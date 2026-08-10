# Deployment

Compose files, container images, provisioning assets, and the ingress that puts
the platform on a network.

Root `compose.yaml` is the canonical local stack. It stays aligned with the
Pydantic settings that read it, provides the real backing services rather than
substitutes, and gives every service an explicit owner and health check.
`README.md` here and the subdirectory READMEs are the operator runbooks.

- Never commit secrets, generated credentials, or local state. A value that
  identifies a resource may live here; a value that authenticates to one may
  not, and belongs in a file the deployment points at.
- While predeployment, maintain one fresh-install database baseline and no
  historical transition paths.
- A deployment value is usually read on several independent paths, so correcting
  one place proves nothing about the rest. When a name, origin, or credential
  changes, find every consumer of it in the same change.
- A stack that reports healthy is not a stack that works. A health check
  describes a process, not the path through it. Confirm the boundary you changed
  from end to end rather than trusting aggregate status.
- Sidecars that share another service's network namespace are destroyed when
  that service is recreated, and the stack will not say so. Treat the lifetime
  relationship as part of the change, not as something to rediscover.
- Two workers share this host and nothing coordinates them: the `container-worker`
  service is the shared platform fleet, and the `agent` service runs one machine a
  customer account joined. Each allocates container addresses inside its own
  control-plane scope, keyed on its own machine id, so a bridge name or subnet used
  twice is two allocators issuing one address with no lock between them. Machine
  fingerprint, state directory, pool, and bridge are the four values that must
  differ, and none of them fails visibly when it does not.
- Operator documentation is part of the change: when deployment behavior
  changes, the runbook that describes it changes with it.
