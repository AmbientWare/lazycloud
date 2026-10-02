#!/bin/sh
# A/B check of one rewrite checkout on a fresh stack: deploy, cold and warm
# remote, warm and cold endpoints. Results append to
# $LCBENCH_STATE_ROOT/results/new.jsonl.
# Usage: ab.sh <checkout> <state root>
set -eu
tree=$1
root=$2
bench=$tree/acceptance/bench
export LCBENCH_STATE_ROOT=$root LCBENCH_STATE=$root/new
mkdir -p "$root/new"
"$bench/rewrite-stack.sh" start >"$root/start.log" 2>&1
run() { "$bench/sdk.sh" new python -m scenarios "$@" >/dev/null 2>&1; }
run deploy 2
run remote 1 0
run remote 5 50
run remote 5 50
run endpoint-warm 1000
run endpoint-cold 10
docker exec lcbench-new-postgres-1 psql -U lazycloud -d lazycloud -c "
select w.name, s.stage, count(*),
       round(percentile_cont(0.5) within group (order by extract(epoch from s.finished_at - s.started_at))::numeric * 1000) as p50_ms
from container_startup_stages s join containers c on c.id = s.container_id
join releases r on r.id = c.release_id join workloads w on w.id = r.workload_id
group by 1, 2 order by 1, 2" >"$root/stages.txt"
docker exec lcbench-new-postgres-1 psql -U lazycloud -d lazycloud -c "
select round(percentile_cont(0.5) within group (order by extract(epoch from a.started_at - t.created_at))::numeric * 1000, 2) as queue_ms,
       round(percentile_cont(0.5) within group (order by extract(epoch from a.finished_at - a.started_at))::numeric * 1000, 2) as exec_ms,
       round(percentile_cont(0.5) within group (order by extract(epoch from t.finished_at - a.finished_at))::numeric * 1000, 2) as commit_ms
from tasks t join attempts a on a.task_id = t.id join workloads w on w.id = t.workload_id
where w.name = 'hold' and a.started_at - t.created_at < interval '1 second'" >>"$root/stages.txt"
"$bench/rewrite-stack.sh" down >/dev/null 2>&1
