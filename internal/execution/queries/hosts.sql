-- name: DrainContainersOnHost :many
-- Starting and ready containers on the host stop claiming; the host stops
-- them once their running attempts finish.
update containers
set state = 'draining', drain_started_at = now()
where host_id = @host_id and state in ('starting', 'ready')
returning id, release_id, image_build_id;

-- name: StoppedContainersAmong :many
select id from containers where id = any(@ids::uuid[]) and state = 'stopped';
