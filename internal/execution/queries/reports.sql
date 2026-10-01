-- name: MarkContainerReady :exec
update containers set state = 'ready', ready_at = now() where id = @id and state = 'starting';

-- name: ResetStartFailures :exec
update releases set start_failures = 0, load_error = null
where id = @id and (start_failures <> 0 or load_error is not null);

-- name: LiveContainersOnHost :many
select id, state from containers where host_id = @host_id and state <> 'stopped' order by id;

-- name: AttemptStates :many
select id, container_id, state from attempts where id = any(@ids::uuid[]);

-- name: OmittedRunningAttempts :many
-- Running attempts on the container that the host's report omits: the host
-- never received them or already lost them. A claim committed within a
-- minute of the report may still be on its way to the host (the agent's
-- claim call times out after 30 s), so only older attempts count.
select id from attempts
where container_id = @container_id
  and state = 'running'
  and started_at < sqlc.arg(observed_at)::timestamptz - interval '60 seconds'
  and not (id = any(@reported::uuid[]))
order by id;
