# Local services

`docker compose up -d --wait postgres object-store` starts PostgreSQL on
127.0.0.1:25432 and Garage's S3 API on 127.0.0.1:23900.
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
