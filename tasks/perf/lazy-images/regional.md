# Regional layer copies

## Scope

Hosts read layer frames from their own region. Today the platform bucket is
in us-east-1 while the fleet buys in us-east-2 first, so most frame fetches
cross regions (about 12 to 15 ms more each from us-east-2, 60 to 70 ms from
the west), where beta9 reads from its own locality.

- A dedicated `layers` bucket in us-east-1 (versioned, noncurrent versions
  expire after a day), replicated by S3 Cross-Region Replication with
  Replication Time Control to one bucket per fleet region.
- Grants presign against the host's own region once the copy there is
  confirmed: the server checks a replica once and records it in Postgres
  (migration 0007); until then it presigns the main bucket.
- Local development keeps one Garage bucket and no replicas.

## Evidence to record

- Owner tests for which bucket a grant names before and after a replica is
  confirmed.
- On EC2 in us-east-2 and us-west-1: frame fetch latency from the regional
  copy against us-east-1.

## Progress

On `perf-integration`, 2026-10-05.

- Terraform: `local.fleet_regions` in fleet.tf is the one region list. The
  fleet networks (`module.fleet`, for_each, the provider's per-resource
  `region`; `moved` blocks from the old per-region modules; the provider
  aliases are gone) and `aws_s3_bucket.layers` (one bucket per fleet
  region) derive from it, with public access blocked, versioning, SSE-S3
  and a lifecycle that aborts incomplete uploads and expires noncurrent
  versions after a day. `aws_s3_bucket_replication_configuration.layers`
  has one rule per other region (RTC 15 minutes, delete markers
  replicated) under role `<deployment>-layer-replication`. The server role
  gets `Layers`, `ListLayers` and `ReadLayerReplicas`. A new region is one
  entry in `fleet_regions` (and its node images) and an apply.
- Config: `LAZYCLOUD_OBJECT_STORE_LAYER_BUCKET` /
  `-object-store-layer-bucket` (server and scheduler; Garage
  `lazycloud-layers` locally) and `LAZYCLOUD_OBJECT_STORE_LAYER_REPLICAS` /
  `-object-store-layer-replicas`, JSON region to bucket (server only;
  unset locally).
- Server: migration 0007 `image_layer_replicas` (layer, region,
  checked_at, confirmed_at; cascades with its layer row, so the sweep's
  retire drops it). `images.LayerReadURLs` takes the host and presigns the
  region's copy for confirmed layers, the layer bucket otherwise, and
  names the region while any is unconfirmed. The session then starts
  `images.ConfirmReplicas` in the background (one per reference and
  region, at most 4 per server, waited by `Server.Wait`): it claims each
  unconfirmed layer's check in one insert, so across replicas a layer is
  checked once per minute at most, HEADs index and data in the copy, and
  records those present. The next grant or refresh reads the copy.

## Evidence

`go test -race`, Postgres from `compose.test.yaml`, own Garage (compose
project `perf-regional`, ports 24970 and 24973):

- `TestGrantsReadTheirRegionsCopyOnceConfirmed` (images): layer bucket
  before any check, before the copy exists, and within the recheck period
  after it does; one layer from the copy once its check passes, both once
  both pass; a region without a copy always reads the layer bucket; the
  sweep leaves no check rows. Fails with a claim that ignores the period.
- `TestReplicaChecksRunOncePerLayerAcrossServers` (images): 8 concurrent
  checks over two servers check 3 layers in total.
- `TestGrantsMoveToTheRegionsCopyOnceConfirmed` (hostsession): the start's
  grant reads the layer bucket, the refresh after the background check
  reads the copy. Fails without the background check.

## Gaps and unverified boundaries

- No `terraform plan` was run: the `moved` blocks and the switch from
  provider aliases to per-resource `region` should plan no change to the
  fleet networks; check that before the apply.
- EC2 frame latency from the copies and S3 replication itself are
  unmeasured.
- A layer's first starts in a region read across regions until its copy
  is confirmed and the grant refreshes (up to a third of the grant life).
