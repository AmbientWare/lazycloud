# LazyCloud

LazyCloud runs Python functions, endpoints, cron jobs, pods and sandboxes on
cloud CPUs and GPUs. A decorator names what the code needs; LazyCloud builds
the image, places containers on capacity and stops them when the work is
done. User documentation lives in [docs/](docs/index.mdx).

## Layout

- `cmd/` and `internal/`: the Go server, scheduler, host agent and
  in-container supervisor, one package per owner.
- `contracts/`: the public OpenAPI document, the host protocol and the local
  runner protocol. Go, Python and TypeScript bindings are generated from them.
- `migrations/`: the PostgreSQL schema.
- `python/lazycloud`: the SDK and `lazycloud` CLI. `python/runner`: the runner
  that executes user code inside containers.
- `web/`: the dashboard.
- `deploy/`: images, the Helm chart, Terraform and the local stack.
- `acceptance/`: cross-owner Go tests against real services.

[AGENTS.md](AGENTS.md) describes the architecture and development rules.

## Develop

```sh
deploy/local/run.sh start      # local stack; prints the SDK environment
./check.sh                     # every formatter and linter, as CI runs them
docker compose -f compose.test.yaml up -d --wait
go test -race ./...
uv sync --group dev && uv run --group dev pytest -x
(cd web && bun install && bun run dev)   # dashboard against 127.0.0.1:8080
(cd web && bun run test)
```

[deploy/local/README.md](deploy/local/README.md) covers the local stack and
[deploy/README.md](deploy/README.md) the production deployment.
