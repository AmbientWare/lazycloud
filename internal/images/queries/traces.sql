-- name: StartupTrace :one
select layers::int[] as layers, frames::int[] as frames, recorded_at from image_traces
where reference = @reference and workspace_id = @workspace_id;

-- name: RecordTrace :one
-- Whether every read names a frame of reference's layer rows. A trace that
-- fits is stored unless one younger than max_age_seconds is.
with layers as (
    select array_agg(l.frames order by r.position) as frames
    from image_reference_layers r
    join image_layers l on l.id = r.layer_id
    where r.reference = @reference
), fits as (
    select coalesce(bool_and(t.layer >= 0 and t.layer < cardinality(layers.frames)
                             and f.frame >= 0 and f.frame < layers.frames[t.layer + 1]), false) as ok
    from layers, unnest(@layers::int[]) with ordinality as t (layer, n)
    join unnest(@frames::int[]) with ordinality as f (frame, n) using (n)
), stored as (
    insert into image_traces (reference, workspace_id, layers, frames)
    select @reference, @workspace_id, @layers::int[], @frames::int[] from fits where fits.ok
    on conflict (reference, workspace_id) do update
    set layers = excluded.layers, frames = excluded.frames, recorded_at = now()
    where image_traces.recorded_at < now() - make_interval(secs => @max_age_seconds::float8)
)
select ok::bool from fits;
