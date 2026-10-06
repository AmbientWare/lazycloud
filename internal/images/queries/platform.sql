-- name: PlatformImages :many
-- The architecture of host, if any, with the recorded state of each of refs
-- for it and of each platform image whose mirror is among mirrors:
-- converted once the mirror has layer rows. The first row carries the
-- architecture even when no image matches.
select h.architecture as host_architecture, p.reference, p.architecture, p.mirror, p.failure,
       p.failure_transient, p.failed_at, p.traceparent,
       (p.mirror is not null and exists (select 1 from image_reference_layers r where r.reference = p.mirror))::bool as converted
from (select @host::uuid as id) q
left join hosts h on h.id = q.id
left join platform_images p
    on (p.reference = any(@refs::text[]) and p.architecture = h.architecture) or p.mirror = any(@mirrors::text[]);

-- name: ClaimPlatformImage :one
-- Takes the conversion lease of a platform image unless another owner
-- holds it or its last attempt failed within its retry period, and
-- returns the mirror recorded before and whether it has layer rows. No row
-- means not claimed.
insert into platform_images (reference, architecture, lease_token, leased_until, traceparent)
values (@reference, @architecture, @token::uuid, now() + make_interval(secs => @lease_seconds::float8), sqlc.narg(traceparent)::text)
on conflict (reference, architecture) do update
set lease_token = excluded.lease_token, leased_until = excluded.leased_until, traceparent = excluded.traceparent
where (platform_images.leased_until is null or platform_images.leased_until < now())
  and (platform_images.failed_at is null or platform_images.failed_at < now() - make_interval(secs => case
      when platform_images.failure_transient then @transient_retry_seconds::float8 else @failure_retry_seconds::float8 end))
returning mirror,
    (mirror is not null and exists (select 1 from image_reference_layers r where r.reference = mirror))::bool as converted;

-- name: FinishPlatformImage :exec
-- Records the converted mirror of the lease token's conversion.
update platform_images
set mirror = @mirror::text, lease_token = null, leased_until = null, failure = null, failure_transient = false, failed_at = null
where reference = @reference and architecture = @architecture and lease_token = @token::uuid;

-- name: FailPlatformImage :exec
-- Records why the lease token's conversion failed and ends its lease.
update platform_images
set lease_token = null, leased_until = null, failure = @failure::text, failure_transient = @transient, failed_at = now()
where reference = @reference and architecture = @architecture and lease_token = @token::uuid;

-- name: RenewPlatformImage :execrows
-- Extends the lease token's conversion lease.
update platform_images set leased_until = now() + make_interval(secs => @lease_seconds::float8)
where reference = @reference and architecture = @architecture and lease_token = @token::uuid;
