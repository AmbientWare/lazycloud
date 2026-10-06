-- name: StartupTrace :one
select layers::int[] as layers, frames::int[] as frames, recorded_at from image_traces
where reference = @reference and workspace_id = @workspace_id;

-- name: ReferenceFrames :many
-- The frame count of each layer of a reference, in the image's order.
select l.frames from image_reference_layers r
join image_layers l on l.id = r.layer_id
where r.reference = @reference
order by r.position;

-- name: RecordTrace :execrows
-- Stores a trace unless a newer one than max_age_seconds is stored. A
-- reference with no recorded use has started nowhere; its trace is
-- dropped.
insert into image_traces (reference, workspace_id, layers, frames)
select u.reference, @workspace_id, @layers::int[], @frames::int[]
from image_reference_uses u where u.reference = @reference
on conflict (reference, workspace_id) do update
set layers = excluded.layers, frames = excluded.frames, recorded_at = now()
where image_traces.recorded_at < now() - make_interval(secs => @max_age_seconds::float8);
