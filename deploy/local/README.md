# Local stack

`deploy/local/run.sh start` starts the services below, builds the binaries
and the managed Python runtime, migrates, creates the `dev` user, workspace
and token, and runs the server, scheduler and agent as host processes. It
prints the SDK environment to export; `deploy/local/run.sh stop` ends them.
State, logs and credentials live in `.lazycloud/`.

With that environment exported, `lazycloud deploy` and the SDK work against
the stack, and `lazycloud machine join --name m1 --workspaces dev` enrolls
another machine from the agent release run.sh published. The end-to-end
Python checks run with `LAZYCLOUD_TEST_ENDPOINT`, `LAZYCLOUD_TEST_TOKEN` and
`LAZYCLOUD_TEST_WORKSPACE` set to the exported values: `uv run --group dev
pytest -x -s python/tests/acceptance`.

## Services

`docker compose up -d --wait postgres object-store` starts PostgreSQL on
127.0.0.1:25432 and Garage's S3 API on 127.0.0.1:23900 and on the Docker
bridge gateway (`LAZYCLOUD_DOCKER_BRIDGE_IP`, 172.17.0.1 by default). The
platform presigns URLs for the gateway address, which workload containers can
reach.
`docker compose run --rm object-store-bootstrap` creates the `lazycloud` bucket
and the development key. Both steps are idempotent.

Owner tests use `docker compose -f compose.test.yaml up -d --wait`, a disposable
PostgreSQL on 127.0.0.1:15442.

Each binary serves Prometheus metrics at `/metrics` on `LAZYCLOUD_METRICS_ADDR`
when it is set, and exports traces over OTLP/gRPC to `LAZYCLOUD_OTLP_ENDPOINT`
(`LAZYCLOUD_OTLP_INSECURE=true` for a local collector). Both are off by
default. `LAZYCLOUD_LOG_FORMAT` picks `text` or `json` logs.

gVisor, devbox disks and memory snapshots need host pieces that only root
can install: `sudo deploy/local/host-setup.sh` installs gVisor's runsc as the
Docker runtime `runsc`, nbd-client and the nbd module. Then
`LAZYCLOUD_OCI_RUNTIME=runsc deploy/local/run.sh start` runs workloads under
gVisor, and `LAZYCLOUD_AGENT_AS_ROOT=1` leaves the agent for you to start with
sudo, which disks and snapshots need.
