-- The measured bytes of the platform volumes among a release spec's volume
-- mounts in a workspace, which a start reads from the workspace's bucket;
-- null when it mounts none. Cloud buckets are the user's and unmeasured.
create function volume_read_bytes(workspace uuid, mounts jsonb) returns bigint
language sql stable
return (
    select sum(v.size_bytes)::bigint
    from volumes v
    where v.workspace_id = workspace and v.state = 'active'
      and v.name in (select m ->> 'name' from jsonb_array_elements(mounts) m where m -> 'cloud_bucket' is null)
);
