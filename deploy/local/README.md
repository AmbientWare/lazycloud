# Local stack

`deploy/local/run.sh start` starts the services below, builds the binaries
and the agent release with the managed Python runtime, migrates, creates
the `dev` user, workspace and token, runs the server and scheduler on this
machine and the agent on the host VM. It prints the SDK environment to
export; `deploy/local/run.sh stop` ends them and stops the VM. State, logs
and credentials live in `.lazycloud/`.

## Host VM

Workloads run in a Lima VM from Amazon Linux 2023 that
`deploy/local/host-vm.sh` sets up with the node image recipe
(`deploy/ami/node-setup.sh`): Docker on the containerd image store with
lazycloud-snapshotter as its storage driver, gVisor, the disk engine's
tools. This machine's Docker is left as it is. The first `up` downloads the
pinned Lima release and image into `.lazycloud/` and builds the host, which
takes several minutes; later ones start it and install the current agent
release the way hosts join. It needs KVM (the `kvm` group) and
`qemu-system-x86_64`, not root. `host-vm.sh down` stops the VM and `reset`
deletes it. Lima's socket paths must stay short: a checkout with a long
path sets `LAZYCLOUD_LIMA_HOME` to a short directory.

The VM reaches the server, registry and Garage on this machine through
forwards to Lima's host address; presigned URLs name the Docker bridge
address, which the VM answers too.

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
