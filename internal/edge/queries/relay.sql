-- name: RegisterEdge :exec
insert into edges (id, relay_address, token_sha256, expires_at)
values (@id, @relay_address, @token_sha256, @expires_at)
on conflict (id) do update set relay_address = excluded.relay_address, expires_at = excluded.expires_at;

-- name: ForgetEdge :exec
delete from edges where id = @id;

-- name: ForgetExpiredEdges :exec
-- Their host links go with them.
delete from edges where expires_at < now();

-- name: EdgeToken :one
select token_sha256, expires_at from edges where id = @id and expires_at > now();

-- name: LinkHost :exec
insert into host_data_links (host_id, edge_id) values (@host_id, @edge_id)
on conflict (host_id) do update set edge_id = excluded.edge_id, updated_at = now();

-- name: UnlinkHost :exec
-- Only this edge's link: the agent may have reconnected to another.
delete from host_data_links where host_id = @host_id and edge_id = @edge_id;

-- name: HostEdge :one
select e.id, e.relay_address
from host_data_links l
join edges e on e.id = l.edge_id
where l.host_id = @host_id and e.expires_at > now();

-- name: LinkHosts :exec
-- Restates every host whose Listen call this edge holds, so links lost
-- with an expired registration come back.
insert into host_data_links (host_id, edge_id)
select h.id, @edge_id from hosts h where h.id = any(@host_ids::uuid[])
on conflict (host_id) do update set edge_id = excluded.edge_id, updated_at = now();
