# Reusable Packages

Package names and imports follow real owners. Public APIs live in `lazycloud`,
boundary contracts in `shared`, durable mapping in `database`, reusable domain
decisions in focused packages, app-private composition in `apps/*`, and provider
adapters in `packages/providers/*`. Do not add catch-all owners, transition
namespaces, aliases, or compatibility packages. Boundary moves update metadata,
callers, tests, docs/examples, entrypoints, and `uv.lock` together.
