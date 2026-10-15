-- The objects of its platform volumes a workload's container read through
-- its first task or request, in first-read order, each
-- volumes/<volume_ids[n]>/<paths[n]>. A later start of the workload warms
-- them before its image pull. The record names only volumes the reporting
-- container mounted in its workspace, and is recorded again once a day.
create table volume_traces (
    workload_id uuid primary key references workloads (id) on delete cascade,
    workspace_id uuid not null,
    volume_ids uuid[] not null,
    paths text[] not null,
    recorded_at timestamptz not null default now(),
    check (cardinality(volume_ids) between 1 and 256 and cardinality(paths) = cardinality(volume_ids))
);
