# Deployable Apps

`apps/` owns process composition, settings, request/command adapters, and
lifetime. Reusable workflow decisions belong in domain packages. When an
entrypoint moves, update its scripts, probes, manifests, packaging, and callers
together; do not leave aliases.
