-- A launch EC2 refused in every pool for capacity fails as no_capacity.
alter table hosts drop constraint hosts_failure_check;
alter table hosts add constraint hosts_failure_check check (failure in (
    'agent_download_failed', 'runtime_install_failed', 'network_join_failed', 'provider_identity_failed',
    'agent_enrollment_failed', 'worker_image_pull_failed', 'worker_start_failed',
    'worker_readiness_failed', 'bootstrap_timed_out', 'host_preflight_failed', 'service_lost',
    'machine_record_deleted', 'provider_stopped', 'provider_terminated', 'no_capacity', 'unknown'
));

-- Each phase and capacity state a host enters, with when: wakes, time
-- asleep, time to ready and what a host cost are read from it. Written by
-- the triggers below, in the transaction that changes the host, and
-- purged by age.
create table host_phase_changes (
    host_id uuid not null references hosts (id) on delete cascade,
    at timestamptz not null,
    phase text not null,
    capacity_state text not null,
    primary key (host_id, at, phase, capacity_state)
);
create index host_phase_changes_at on host_phase_changes (at);

create function record_created_host_phases() returns trigger
language plpgsql as $$
begin
    insert into host_phase_changes (host_id, at, phase, capacity_state)
    select n.id, now(), n.phase, n.capacity_state from new_rows n
    on conflict do nothing;
    return null;
end $$;

create function record_updated_host_phases() returns trigger
language plpgsql as $$
begin
    insert into host_phase_changes (host_id, at, phase, capacity_state)
    select n.id, now(), n.phase, n.capacity_state
    from new_rows n join old_rows o on o.id = n.id
    where n.phase <> o.phase or n.capacity_state <> o.capacity_state
    on conflict do nothing;
    return null;
end $$;

create trigger hosts_phase_created after insert on hosts
    referencing new table as new_rows for each statement execute function record_created_host_phases();
create trigger hosts_phase_updated after update on hosts
    referencing old table as old_rows new table as new_rows for each statement execute function record_updated_host_phases();
