-- Platform hosts that still exist, by id: the admin Nodes page and the agent
-- rollout read them without walking deleted host history, which is never
-- purged.
create index hosts_platform on hosts (id) where kind = 'platform' and phase <> 'deleted';
