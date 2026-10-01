-- name: ClaimRoute :exec
-- The unique subdomain and hostname columns reject a claim another workload
-- already holds; the caller maps the violation to a conflict.
insert into http_routes (workload_id, subdomain, hostname)
values (@workload_id, @subdomain, sqlc.narg(hostname))
on conflict (workload_id) do update set subdomain = excluded.subdomain, hostname = excluded.hostname;

-- name: OwnerRegisteredDomain :one
-- Registrations belong to accounts; any workspace an account owns may serve
-- them. A registration still waiting for its certificate counts.
select exists (
    select 1
    from custom_domains d
    join workspace_members m on m.user_id = d.user_id and m.role = 'owner'
    where m.workspace_id = @workspace_id and d.hostname = @hostname
)::bool;
