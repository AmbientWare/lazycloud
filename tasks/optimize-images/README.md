# Image work optimization pass

Same product, less code. Full review, refactor and rewrite where it pays, of
everything the image work added (commits 5420ac91, 7ae7e3ff, 4644ed4f,
b1176232 on main). The spec is main at b1176232, checked out read-only at
/tmp/lc-spec. Nothing deploys until the pass lands.

## Baseline (b1176232)

Lines the image work added, generated code (sqlc, protobuf, API bindings)
excluded: 11,349 production, 7,724 test. Largest production files:
internal/images/platform.go 627, layers.go 587, internal/agent/convert.go 485,
internal/imagefs/snapshotter/cache.go 420, internal/imagefs/convert.go 416,
snapshotter/trace.go 392, internal/agent/layers.go 383, snapshotter/mounts.go
382, snapshotter/snapshotter.go 366, internal/images/convert.go 337.

Performance to hold (EC2, m7i.large, torch CPU image): cold start to the end
of the first task 4.46 s with a trace; torch build after install 24.7 s;
python cold start on the uv base; first container after resume 0.4 s.

## Parity inventory

### Format and upload
- [ ] Layer to index plus data of 4 MiB checksummed zstd frames, keyed by diff_id (internal/imagefs/convert.go, index.go)
- [ ] Upload pair: bounded parallel parts, index last, a refused part stops the rest (internal/imagefs/upload.go)

### Build and publish
- [ ] Two-pass BuildKit build: push, then cache export alongside conversion; cache export failure after push does not fail the build; digest from metadata (internal/agent/build.go)
- [ ] Conversion of the layers the server names, one layer uploading while another converts, bounded rounds (internal/agent/convert.go, internal/images/convert.go)
- [ ] zstd layer and cache compression

### Grants and copies
- [ ] Presigned per-layer GET grants, 1 h life, refreshed at a third (internal/images/layers.go, internal/hostsession/layers.go)
- [ ] Regional copies used once HeadObject confirms them (image_layer_replicas)
- [ ] Layers bucket with replication to every fleet region (deploy/terraform/platform-deployment)

### Snapshotter
- [ ] Containerd proxy snapshotter, FUSE layer mounts, mount reconciliation (internal/imagefs/snapshotter)
- [ ] Bounded LRU frame cache, small-layer fills
- [ ] Prefetch from startup traces; shared-layer rule; refreshes do not void traces
- [ ] Metrics and spans

### Agent
- [ ] Pulls through containerd into the moby namespace
- [ ] Startup trace from start to the first task, request or pod command exit, 60 s cap (internal/agent/layers.go, container.go, data.go, pod.go)
- [ ] Grant refresh loop with bounded retry
- [ ] Platform image readiness for mount and builder containers (internal/agent/platform.go)
- [ ] Sync drops changed modules' cached bytecode (internal/agent/sync.go)

### Server images
- [ ] Platform image allowlist and server conversion; managed Python images converted at start, fleet architectures only (internal/images/platform.go, internal/platformimages)
- [ ] Startup traces stored per workspace and image (internal/images/traces.go)
- [ ] Renderer: uv-managed base by minor version, exact patch installs, uv pip install --compile-bytecode for every pip-style step (internal/images/render.go)

### Lifecycle and API
- [ ] Image conversion lifecycle stage from assignment to start sent or failed (internal/hostsession/observations.go, contracts/openapi.yaml, web lifecycle strip)

### Tracing
- [ ] Spans across server, scheduler, agent and snapshotter; traceparents on rows and host commands; host span intake with per-host token bucket; redaction of presigned URLs (internal/telemetry, internal/hostsession/tracing.go)
- [ ] Collector: per-process X-Ray segments, lazycloud.span annotation, metrics port (deploy/helm/lazycloud)

### Images and hosts
- [ ] Python 3.10 to 3.14 bases, reproducible digests, built and pushed by Ship (deploy/images)
- [ ] Node images ship the snapshotter; default hibernation image size; version prints without SIGPIPE (deploy/ami)
- [ ] Tests use the test Garage and delete their buckets and keys (internal/storage/storagetest)

## Rules for every packet

- Never name other platforms in code, comments, commits, branches or PR text.
- 0003 and 0004 are deployed and frozen. 0005 is undeployed: change it in place; add no other migration.
- Commit after each step, one-line subjects, no attribution trailers.

## Packets

| Packet | Branch | Owns |
| --- | --- | --- |
| snapshotter | opt-snapshotter | internal/imagefs/snapshotter, cmd/snapshotter, cmd/agent/snapshotter.go |
| hostsession | opt-hostsession | internal/hostsession, storage links and credentials, stage comments |
| imagefs | opt-imagefs | internal/imagefs (not snapshotter), storage/layers.go, the shared convert-then-upload helper and its two callers |
| images | opt-images | internal/images, internal/platformimages, migration 0005 |
| agent | opt-agent | internal/agent (not the convert-then-upload code) |
| tracing and deploy | opt-ops | internal/telemetry, collector chart, Terraform, workflows, base images |

## Integrator, after the packets

- One layer grant and frame read shape: host.proto imports the imagefs
  LayerGrant and FrameRead; delete layersource.Grant, FrameRead, readsOut and
  the agent's grantsIn. Same wire format.
