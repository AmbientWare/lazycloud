# Local stack

`deploy/local/run.sh start` starts the services below, builds the binaries
and the agent release with the managed Python runtime, migrates, and creates
the `dev` user, workspace and token. The server and scheduler run on this
machine and the agent runs on the host VM. It prints the SDK environment to
export; `deploy/local/run.sh stop` ends them and stops the VM. State, logs
and credentials live in `.lazycloud/`.

## Host VM

Workloads run in a Lima VM that `deploy/local/host-vm.sh` builds from Amazon
Linux 2023 with the node image recipe (`deploy/ami/node-setup.sh`), so it
runs the same Docker, gVisor and lazycloud-snapshotter as fleet hosts. This
machine's Docker stays as it is. The script needs KVM (the `kvm` group) and
`qemu-system-x86_64`, not root.

- `up` creates or starts the VM and installs the current agent release the
  way hosts join. The first one downloads Lima and the image into
  `.lazycloud/` and takes several minutes.
- `down` stops the VM; `reset` deletes it.
- Agent updates leave a running snapshotter alone. `up` replaces the VM's
  snapshotter with this tree's build only while no container runs there;
  otherwise `reset` gives a fresh host.
- Lima's socket paths must stay short. In a deep checkout, point
  `LAZYCLOUD_LIMA_HOME` at a short directory.

The VM reaches the server, registry and Garage through forwards to Lima's
host address. Presigned URLs name the Docker bridge address, which the VM
answers too.

With that environment exported, `lazycloud deploy` and the SDK work against
the stack, and `lazycloud machine join --name m1 --workspaces dev` enrolls
another machine from the agent release run.sh published. A join runs as root
and switches that machine's Docker to the snapshotter, restarting it
(docs/platform/compute.mdx says how to revert), so the VM is the easier host.
The end-to-end Python checks run with `LAZYCLOUD_TEST_ENDPOINT`,
`LAZYCLOUD_TEST_TOKEN` and `LAZYCLOUD_TEST_WORKSPACE` set to the exported
values: `uv run --group dev pytest -x -s python/tests/acceptance`.

## Services

`docker compose up -d --wait postgres object-store` starts PostgreSQL on
127.0.0.1:25432 and Garage's S3 API on 127.0.0.1:23900 and on the Docker
bridge gateway (`LAZYCLOUD_DOCKER_BRIDGE_IP`, 172.17.0.1 by default). The
platform presigns URLs for the gateway address, which workload containers can
reach.
`docker compose run --rm object-store-bootstrap` creates the `lazycloud` and
`lazycloud-layers` buckets and the development key. Both steps are idempotent.

Tests use `docker compose -f compose.test.yaml up -d --wait`, a disposable
PostgreSQL on 127.0.0.1:15442 and Garage on 127.0.0.1:15900 (admin API on
15903), never this stack. Each test makes its own buckets there and deletes
them when it ends.

Each binary serves Prometheus metrics at `/metrics` on `LAZYCLOUD_METRICS_ADDR`
when it is set, and exports traces over OTLP/gRPC to `LAZYCLOUD_OTLP_ENDPOINT`
(`LAZYCLOUD_OTLP_INSECURE=true` for a local collector). Both are off by
default. `LAZYCLOUD_LOG_FORMAT` picks `text` or `json` logs.

run.sh sends traces to the `jaeger` service at http://127.0.0.1:16686; the VM's
agent and snapshotter send theirs through the server, as fleet hosts do.
