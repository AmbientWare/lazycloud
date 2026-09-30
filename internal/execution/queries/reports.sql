-- name: MarkContainerReady :exec
update containers set state = 'ready', ready_at = now() where id = @id and state = 'starting';

-- name: ResetStartFailures :exec
update releases set start_failures = 0 where id = @id and start_failures <> 0;

-- name: LiveContainersOnHost :many
select id, state from containers where host_id = @host_id and state <> 'stopped' order by id;

-- name: AttemptStates :many
select id, container_id, state from attempts where id = any(@ids::uuid[]);
