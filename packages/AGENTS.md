# Reusable packages

Domain packages: the reusable decisions the deployable apps compose.

Names and imports follow real owners. Public APIs live in `lazycloud`, boundary
contracts in `shared`, durable mapping in `database`, provider adapters under
`providers/`, reusable domain decisions in the focused package the behavior is
about, and app-private composition in `apps/*`.

Do not add catch-all owners, transition namespaces, aliases, or compatibility
packages. Each one forces the next reader to work out which of two homes is real,
and both stay half-maintained until someone does.

That is a rule about ownership, not about file names. Reach is what decides where
something lives: a helper several packages genuinely depend on belongs to a
package that owns it for all of them, and one only its own package calls stays
private to that package however generic it looks. Duplicating a function across
packages to avoid the question is the failure this prevents; so is hoisting a
single-caller helper somewhere central on the argument that it might be reused.
The name to avoid is the one that describes no domain, such as `utils`,
`helpers`, or `common`, because nothing can be said to own it and nothing can be
said not to belong in it.

Moving a boundary updates package metadata, callers, tests, docs and examples,
entrypoints, and the lockfile together.
