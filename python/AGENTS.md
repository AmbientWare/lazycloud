# Python

- `lazycloud` is the public SDK and CLI, `runner` executes user code inside
  containers and `shared` holds their wire contracts. None imports backend code.
- Give reusable behavior one owner. Keep single-consumer helpers private; avoid
  catch-all utilities, transition namespaces and compatibility aliases.
- Boundary moves update metadata, callers, tests, docs, entrypoints and lockfiles
  together.
