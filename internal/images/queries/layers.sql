-- name: ReferenceLayers :many
-- The converted layers of a reference, in the image's order. The rows stay
-- locked until the caller records them for another reference, so the sweep
-- cannot retire them in between.
select l.id, l.blob_digest, l.diff_id, l.workspace_id
from image_reference_layers r
join image_layers l on l.id = r.layer_id
where r.reference = @reference
order by r.position
for key share of l;

-- name: LayerReadsFor :many
-- The converted layers of each of refs, in each image's order, with
-- the host's region and whether that region's copy is confirmed to hold
-- each.
select r.reference, l.id, l.diff_id, coalesce(h.region, '')::text as region, (c.confirmed_at is not null)::boolean as replicated
from image_reference_layers r
join image_layers l on l.id = r.layer_id
left join hosts h on h.id = @host
left join image_layer_replicas c on c.layer_id = l.id and c.region = h.region
where r.reference = any(@refs::text[])
order by r.reference, r.position;

-- name: ClaimReplicaChecks :many
-- Claims the check of every layer of a reference in region's copy that is
-- not confirmed and was not claimed within the retry period, so one server
-- checks each layer and region at a time. Claims take the rows in layer
-- order, so concurrent claims of shared layers wait instead of deadlocking.
insert into image_layer_replicas (layer_id, region, checked_at)
select r.layer_id, @region, now() from image_reference_layers r where r.reference = @reference
order by r.layer_id
on conflict (layer_id, region) do update set checked_at = now()
where image_layer_replicas.confirmed_at is null
    and image_layer_replicas.checked_at < now() - make_interval(secs => @retry_seconds::float8)
returning layer_id;

-- name: ConfirmReplicas :execrows
update image_layer_replicas set confirmed_at = now()
where region = @region and layer_id = any(@layer_ids::uuid[]) and confirmed_at is null;

-- name: ReplicasPending :one
-- Whether region's copy is not confirmed to hold some layer of reference.
select exists (
    select 1 from image_reference_layers r
    left join image_layer_replicas c on c.layer_id = r.layer_id and c.region = @region
    where r.reference = @reference and c.confirmed_at is null
)::boolean;

-- name: UploadsOf :many
-- The uploads offered to a build container for these blobs.
select id, blob_digest, upload_id, data_bytes, index_bytes from image_layer_uploads
where container_id = @container_id and blob_digest = any(@blobs::text[]);

-- name: StartUpload :execrows
-- Records the data upload started for these sizes, unless another report
-- replaced previous meanwhile.
update image_layer_uploads
set upload_id = @upload_id::text, data_bytes = @data_bytes::bigint, index_bytes = @index_bytes::bigint
where id = @id and upload_id is not distinct from sqlc.narg(previous)::text;

-- name: EndContainerUploads :many
-- Ends the uploads a build container no longer makes. The sweep deletes what
-- they stored once no URL issued for them can write again.
update image_layer_uploads set expires_at = least(expires_at, now() + make_interval(secs => @url_seconds::float8))
where container_id = @container_id and expires_at > now()
returning id, upload_id;

-- name: RecordLayer :execrows
-- Records an uploaded pair, unless a pair of the blob already serves the
-- same scope: then the first one stays and this one counts no rows.
insert into image_layers (id, blob_digest, diff_id, workspace_id, frames)
values (@id, @blob_digest, @diff_id, sqlc.narg(workspace_id), @frames)
on conflict do nothing;

-- name: ClaimUpload :exec
delete from image_layer_uploads where id = @id;

-- name: AbandonUpload :exec
-- A pair that lost to another of its blob is deleted once no URL issued for
-- it can write again.
update image_layer_uploads set expires_at = now() + make_interval(secs => @url_seconds::float8) where id = @id;

-- name: UsableLayers :many
-- The pairs an image may use for these blobs: those platform hosts
-- converted (shared), and with a workspace those its own hosts converted.
-- The rows stay locked until the image's layers are recorded, so the sweep
-- cannot take them in between; locking in id order keeps concurrent
-- publishes from deadlocking.
select id, blob_digest, diff_id, (workspace_id is null)::bool as shared from image_layers
where blob_digest = any(@blobs::text[])
  and (workspace_id is null or workspace_id = sqlc.narg(workspace_id)::uuid)
order by id
for key share;

-- name: OfferUpload :one
-- The upload of a blob's pair for a build container, the same one on every
-- report until the pair is recorded.
insert into image_layer_uploads (container_id, blob_digest, expires_at)
values (@container_id, @blob_digest, @expires_at)
on conflict (container_id, blob_digest) do update
set expires_at = greatest(image_layer_uploads.expires_at, excluded.expires_at)
returning id, upload_id, data_bytes, index_bytes;

-- name: RecordReference :exec
-- Records layer_ids as reference's layers, in order, ends their grace
-- period and records the reference's use. The caller holds the layer rows
-- locked, so the sweep cannot retire one in between.
with recorded as (
    insert into image_reference_layers (reference, position, layer_id)
    select @reference::text, l.n - 1, l.id
    from unnest(@layer_ids::uuid[]) with ordinality as l (id, n)
    on conflict do nothing
), touched as (
    update image_layers set unreferenced_since = null
    where id = any(@layer_ids::uuid[]) and unreferenced_since is not null
)
insert into image_reference_uses (reference, used_at)
values (@reference::text, now())
on conflict (reference) do update set used_at = excluded.used_at;

-- name: MarkUnreferencedLayers :exec
-- Starts the grace period of pairs no live reference uses and ends it for
-- pairs one uses again. Live references are those pinned by releases that
-- can still start containers (active releases of workloads and apps not
-- deleted, live previews, releases with containers or tasks under way),
-- the newest converted managed image of each Python version and
-- architecture, and references published or started within the grace
-- period. Each part reads live rows by an index; image history is not
-- read.
with live_releases as (
    select w.active_release_id as id from workloads w
    join apps a on a.id = w.app_id
    where w.desired_state <> 'deleted' and a.state <> 'deleted' and w.active_release_id is not null
    union
    select release_id from previews where stopped_at is null
    union
    select release_id from containers where state <> 'stopped' and release_id is not null
    union
    select release_id from tasks where status in ('queued', 'running')
), live as (
    select r.spec -> 'image' ->> 'reference' as reference from releases r join live_releases l on l.id = r.id
    union
    (select distinct on (m.python_version, p.architecture) p.mirror
     from managed_images m join platform_images p on p.reference = m.source
     where p.mirror is not null
     order by m.python_version, p.architecture, m.created_at desc)
    union
    select u.reference from image_reference_uses u
    where u.used_at > now() - make_interval(secs => @grace_seconds::float8)
), used as (
    select rl.layer_id from image_reference_layers rl join live on live.reference = rl.reference
)
update image_layers
set unreferenced_since = case when id in (select layer_id from used) then null else now() end
where (id in (select layer_id from used)) = (unreferenced_since is not null);

-- name: RecordUses :exec
-- Records that references were published or started, at most once a minute
-- per reference.
insert into image_reference_uses (reference, used_at)
select unnest(@refs::text[]), now()
on conflict (reference) do update set used_at = excluded.used_at
where image_reference_uses.used_at < now() - interval '1 minute';

-- name: PurgeUses :exec
delete from image_reference_uses where used_at < now() - make_interval(secs => @grace_seconds::float8);

-- name: UnreferencedLayers :many
select id from image_layers
where unreferenced_since < now() - make_interval(secs => @grace_seconds::float8)
order by unreferenced_since
limit @batch_size;

-- name: RetireLayer :exec
-- Moves a pair unused through the grace period to image_layer_uploads for
-- deletion, and unconverts every reference that used it, dropping their
-- startup traces: a reference is converted as a whole or not at all. A
-- reference recorded with it meanwhile fails the statement's foreign key
-- check, and a publish that touched it ends the grace period first.
with gone as (
    delete from image_layers
    where image_layers.id = @id and unreferenced_since < now() - make_interval(secs => @grace_seconds::float8)
    returning image_layers.id, image_layers.blob_digest
), refs as (
    delete from image_reference_layers
    where reference in (select r.reference from image_reference_layers r join gone g on g.id = r.layer_id)
    returning reference
), traces as (
    delete from image_traces where reference in (select reference from refs)
)
insert into image_layer_uploads (id, blob_digest, expires_at)
select g.id, g.blob_digest, now() from gone g;

-- name: ExpiredUploads :many
select id, upload_id from image_layer_uploads where expires_at < now() order by expires_at limit @batch_size;

-- name: DeleteUploads :exec
delete from image_layer_uploads where id = any(@ids::uuid[]) and expires_at < now();
