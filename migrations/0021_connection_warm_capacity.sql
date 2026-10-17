-- Whether a connection's markets keep the platform's reserves, a warm host
-- and a stopped one, while a workspace uses the connection.
alter table cloud_connections add column warm_capacity boolean not null default true;
