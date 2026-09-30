# LazyCloud

This branch is the starting point for the Go backend and host runtime.
The replacement backend is not implemented yet.

Read [update.md](update.md) for the architecture, reference implementation,
agent instructions and capability checklist. [AGENTS.md](AGENTS.md) contains
development rules.

The Python SDK and public CLI live in python/lazycloud, the Python runner in
python/runner and their wire contracts in python/shared. The frontend lives in
web and language-neutral contract examples in contracts. Published product
documentation describes the reference platform.

The old backend, internal admin CLI, deployment automation and backend-specific
tests remain available in the pinned reference. The public lazycloud CLI remains;
lazycloud-admin must be rebuilt against explicit administration contracts.

Python development:

```sh
uv sync --group dev
uv run --group dev pytest -x
uv run --group dev lazycloud --help
```

Use Bun from web for frontend development. Backend-dependent UI workflows
need an API implementing their contracts. The new backend may use fresh contracts
and a fresh schema; update these consumers together. Old migration history remains
in the pinned reference, without requiring a data migration or compatibility layer.

Add the Go module with the first implemented workflow. Do not add empty packages
or placeholder services to represent unchecked capabilities.
