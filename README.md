# LazyCloud

This branch is the starting point for the Rust backend and host runtime.
The replacement backend is not implemented yet.

Read [update.md](update.md) for the architecture, reference implementation,
agent instructions and capability checklist. [AGENTS.md](AGENTS.md) contains
development rules.

The Python SDK and public CLI live in packages/lazycloud, the Python runner in
packages/runner, and the frontend in apps/web. Their existing contracts and runner
helpers remain in packages/shared and packages/foundation. Published product
documentation and examples describe the reference platform.

The old backend, internal admin CLI, deployment automation and backend-specific
tests remain available in the pinned reference. The public lazycloud CLI remains;
lazycloud-admin must be rebuilt against explicit administration contracts.

Python development:

```sh
uv sync --group dev
uv run --group dev pytest -x
uv run --group dev lazycloud --help
```

Use Bun from apps/web for frontend development. Backend-dependent UI workflows
need an API implementing their contracts. The new backend may use fresh contracts
and a fresh schema; update these consumers together. Old migration history remains
in the pinned reference, without requiring a data migration or compatibility layer.

Introduce the Cargo workspace with the first implemented Rust workflow. Do not
add empty crates or placeholder services to represent unchecked capabilities.
