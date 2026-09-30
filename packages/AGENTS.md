# Packages

- Give reusable behavior one domain owner. Keep single-consumer helpers private;
  avoid catch-all utilities, transition namespaces and compatibility aliases.
- Public APIs belong in `lazycloud`, contracts in `shared`, persistence mapping
  in `database`, adapters in `providers/*`, and app composition in `apps/*`.
- Boundary moves update metadata, callers, tests, docs, entrypoints and lockfiles
  together.
