-- What a reserve's own agent proved before it stopped and reported after it
-- came back, and how long each fleet activation took.

alter table hosts
    -- The PrepareReserve attempt the agent answered ready, and the boot it
    -- answered in. The resume report naming it settles it once.
    add column sleep_attempt_id uuid,
    add column sleep_boot_id text,
    -- The agent release the host ran when it last proved it could stop.
    add column prepared_agent_version text,
    -- The driver reported every GPU when the host last proved it could stop.
    add column gpu_proven boolean not null default false,
    -- How the host came back from its last stop: with its memory, or booted.
    add column last_resume_outcome text check (last_resume_outcome in ('memory_restored', 'cold_boot'));

-- One sample per platform host activation: a launch until its first
-- session (provision), or a requested resume until the host's Hello, from a
-- hibernation (resume) or a plain stop (boot). Rows older than a day are
-- pruned as new ones arrive.
create table fleet_activations (
    id uuid primary key default uuidv7(),
    kind text not null check (kind in ('provision', 'boot', 'resume')),
    instance_type text not null,
    region text not null,
    gpu_type text not null default '',
    seconds double precision not null check (seconds >= 0),
    outcome text not null check (outcome in ('ready', 'memory_restored', 'cold_boot', 'failed')),
    at timestamptz not null default now()
);

create index fleet_activations_at on fleet_activations (at);
