# Python

- `lazycloud` is the public SDK and CLI and `runner` executes user code inside
  containers. `lazycloud.contracts` and `lazycloud._shared` hold the wire
  contracts and helpers they share. None imports backend code.
- Give reusable behavior one owner. Keep single-consumer helpers private; avoid
  catch-all utilities, transition namespaces and compatibility aliases.
- Boundary moves update metadata, callers, tests, docs, entrypoints and lockfiles
  together.
