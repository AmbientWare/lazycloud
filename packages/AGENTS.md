# Reusable Packages

Domain packages: the reusable decisions the deployable apps compose.

Names and imports follow real owners. Public APIs live in `lazycloud`, boundary
contracts in `shared`, durable mapping in `database`, provider adapters under
`providers/`, reusable domain decisions in the focused package the behavior is
about, and app-private composition in `apps/*`.

Do not add catch-all owners, transition namespaces, aliases, or compatibility
packages. Each one forces the next reader to work out which of two homes is real,
and both stay half-maintained until someone does.

Moving a boundary updates package metadata, callers, tests, docs and examples,
entrypoints, and the lockfile together.
