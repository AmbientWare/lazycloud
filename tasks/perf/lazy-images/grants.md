# Grants

## Scope

A host reads only the layers of images it was told to run. Owns:

- the server side: with each start command, presigned GET URLs for the
  `index` and `data` of every layer of the container's image, and fresh URLs
  before they expire for as long as a container on the host uses the layer
  (the way `StorageGrant` refreshes);
- `contracts/host/v1/host.proto` fields 100-109 for them;
- the agent side: hand the URLs to the snapshotter over the local interface,
  and refresh them. Propose that interface in your first commit (a small
  local gRPC or HTTP API on the snapshotter's socket); the integrator settles
  it with the snapshotter packet.

Stay off the snapshotter's internals, the build path and `internal/imagefs`.

## Plan

- Presigning is local computation; no store call per start. Choose an expiry
  and refresh margin and say why.
- The same path serves platform, connected-account and self-hosted hosts.

## Evidence to record

- A host sent one workspace's image cannot read another layer with what it
  was sent: a URL for one object does not open another, and an expired URL
  stops working.
- A long-running container keeps reading past the first expiry.
- Owner tests for which layers a start command carries, against the real
  test Postgres.

## Progress

On `perf-lazy-grants`, on top of `perf-plan` (60ed11ab, publish merged) and the
snapshotter's contract commit (99558431, replayed).

- `host.proto`: `StartContainer.layers = 100` and `ServerMessage.layer_grants
  = 100` (`LayerGrants`), both of `LayerGrant` (diff_id, index and data URLs,
  expires_at). The host protocol keeps its own message rather than importing
  the snapshotter's contract; the agent copies four fields.
- Server (`internal/hostsession/layers.go`): every start carries
  `images.LayerReadURLs` for the reference the host pulls, pinned or
  managed, and nothing else. One sync signs a reference once for all its
  replicas. Before the start is sent, `execution.RecordImageReference`
  stores that reference on the container (migration 0004,
  `containers.image_reference`). `BuildWaitError`, `ErrNotReady` and
  `ErrNotConverted` wait: the start is not sent, the session wakes on
  `ChannelImageBuild`, and the next sync's pull starts or joins the
  conversion. `ConversionError` fails the start with its reason.
- Refresh: the session records each reference's grant life. It lists the
  live containers' references (`execution.LiveImagesOnHost`, on
  `containers_live_host`) when it opens and when a grant is due, not on
  every sync, sends a `LayerGrants` per reference a third into its life,
  and forgets references no live container runs.
- Agent (`internal/agent/layers.go`): `prepare` hands the start's grants to
  the snapshotter (`layersource.Client.Grant`) before `imageCache.ensure`; a
  refusal fails the start. A start sent again for a known container hands
  its fresh grants over too. Refreshes queue into a map of the newest
  unexpired grant per layer drained by one owned goroutine; a refused batch
  is queued again and retried after 1 s doubling to 30 s. Flag
  `-snapshotter` defaults to `layersource.Socket`; a host without a
  snapshotter fails the snapshotter packet's preflight check.
- Storage credentials: the default chain's cache renews credentials 15
  minutes before they expire, and `signedLifetime` counts that window, so a
  URL signed during rotation lasts at least 14 minutes and never outlives
  its credentials.

### Lifetime

One hour, renewed a third into its life. An hour is what other host
transfers get; the server's role-session credentials may shorten it to no
less than 14 minutes, and renewing by the life the owner reports keeps
working then. Forty minutes of margin covers a control plane restart or a
lost session. The cost of the length: a host can read an image's layers
for up to an hour after its last container of it stops, bytes it already
had. Presigning is local; a sync costs one query per image it starts.

## Evidence

`go test -race` with Postgres from `compose.test.yaml` and a Garage of my
own (compose project `perf-lazy-grants`, ports 24910 and 24913, removed
after):

- `TestStartCarriesOnlyItsImageLayers` (hostsession): images a and b share
  a base layer and run on two hosts. Host one's starts of a carry exactly
  [base, app a] in order, two replicas share one signing, and no grant
  names app b; the managed Python image carries its own layer; a pinned
  reference without converted layers is not sent, its container stays
  starting, and one conversion build runs for it.
- `TestLayerGrantsOpenOnlyTheirObjectAndRefreshWhileUsed` (hostsession,
  3 s lifetime): the granted index reads back byte for byte and the data
  serves a range (206). The same signature on another layer's index, another
  layer's data, or the pair's other object answers 403. A refresh with a
  later expiry arrives before the first expires; the expired URL then
  answers 400 (Garage) while the newest refresh still reads. Once the
  container stops, no grant follows for two lifetimes.
- `TestReconnectedSessionGrantsRunningImages` (hostsession): a reopened
  session grants the managed image of a running container at once and
  nothing for an image only a stopped container used.
- `TestLayerGrantsReachTheSnapshotterBeforeThePull` (agent, real Docker,
  a LayerSources server on a unix socket): the snapshotter holds the start's
  grants before the container reaches STARTING; a refused refresh is
  retried until held; a re-sent start's grant reaches it; a refused start
  grant ends the start `START_FAILED` with no container created.
- `TestSignedURLsOutlastCredentialRotation` (storage): credentials in their
  last 30 s give a URL of more than 13 minutes from renewed credentials,
  never past their expiry. Without the window it fails.
- `./check.sh` passes.

## Intentional differences

## Gaps and unverified boundaries

- Migration 0004 is this packet's; the packet table listed none.
- S3 and a real snapshotter reading past the first expiry are left to
  acceptance.
