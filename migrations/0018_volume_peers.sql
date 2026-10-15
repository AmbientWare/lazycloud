-- The private address other hosts of the region read cached volume frames
-- from, as host:port; empty while the host serves none.
alter table hosts add column peer_address text not null default '';

-- The key hosts holding the workspace's storage grant sign peer frame reads
-- with, made with the workspace's first grant.
alter table workspaces add column volume_peer_key bytea check (octet_length(volume_peer_key) = 32);
