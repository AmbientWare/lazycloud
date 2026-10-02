-- One row per core table, every key the zero UUID the checker binds, so
-- queries reach their per-row joins and subqueries instead of stopping at
-- empty tables.
insert into users (id) values ('00000000-0000-0000-0000-000000000000');
insert into workspaces (id, name) values ('00000000-0000-0000-0000-000000000000', 'neki-check');
insert into workspace_members (workspace_id, user_id, role)
values ('00000000-0000-0000-0000-000000000000', '00000000-0000-0000-0000-000000000000', 'owner');
insert into billing_accounts (user_id) values ('00000000-0000-0000-0000-000000000000');
insert into billing_balances (user_id, month_started_at, recheck_at)
values ('00000000-0000-0000-0000-000000000000', date_trunc('month', now(), 'UTC'), now());
insert into apps (id, workspace_id, name, state)
values ('00000000-0000-0000-0000-000000000000', '00000000-0000-0000-0000-000000000000', 'neki_check', 'active');
insert into workloads (id, app_id, kind, name, desired_state)
values ('00000000-0000-0000-0000-000000000000', '00000000-0000-0000-0000-000000000000', 'endpoint', 'neki_check', 'active');
insert into releases (id, workload_id, version, spec, spec_digest, source_sha256)
values ('00000000-0000-0000-0000-000000000000', '00000000-0000-0000-0000-000000000000', 1, '{}',
        decode(repeat('00', 32), 'hex'), decode(repeat('00', 32), 'hex'));
insert into hosts (id, name, state, cpu_millis, memory_bytes)
values ('00000000-0000-0000-0000-000000000000', 'neki-check', 'online', 1000, 1000);
insert into containers (id, workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, purpose,
                        keep_warm_seconds, active_until)
values ('00000000-0000-0000-0000-000000000000', '00000000-0000-0000-0000-000000000000',
        '00000000-0000-0000-0000-000000000000', 'ready', '00000000-0000-0000-0000-000000000000', 1, 1, 1, 'serve',
        0, now());
insert into tasks (id, workspace_id, workload_id, release_id, status, max_attempts)
values ('00000000-0000-0000-0000-000000000000', '00000000-0000-0000-0000-000000000000',
        '00000000-0000-0000-0000-000000000000', '00000000-0000-0000-0000-000000000000', 'running', 1);
insert into attempts (id, task_id, number, container_id, state, deadline_at)
values ('00000000-0000-0000-0000-000000000000', '00000000-0000-0000-0000-000000000000', 1,
        '00000000-0000-0000-0000-000000000000', 'running', now() + interval '1 hour');
