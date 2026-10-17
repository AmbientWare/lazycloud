-- Whether a connection's markets keep the platform's CPU reserves, a warm
-- host and a stopped one, and its GPU reserves, stopped hosts of each card,
-- while a workspace uses the connection.
alter table cloud_connections
    add column warm_cpu boolean not null default true,
    add column warm_gpu boolean not null default true;
