-- name: AttemptContainer :one
-- An attempt's container never changes, so this read needs no lock. The
-- container state tells whether a finished attempt lets it stop.
select a.container_id, c.state as container_state
from attempts a
join containers c on c.id = a.container_id
where a.id = @id;
