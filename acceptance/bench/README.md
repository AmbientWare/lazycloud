# Benchmarks

A manual A/B harness for two commits of the platform. CI does not run it; the
Go guard tests (per-completion query cost against a growing backlog, idle
passes against scheduler replicas, callback promptness) are the regression
gate.

## Compare two commits

```sh
acceptance/bench/ab.sh <commit-a> <commit-b> [phase...]
```

Each commit gets a detached worktree and a fresh stack on the benchmark ports
(Compose project `lcbench`, API on 28080), runs the phases, and is torn down
before the other starts, workload containers included. Both runs use this
checkout's harness, so the scenarios match. Results land in
`$LCBENCH_ROOT/a.jsonl` and `b.jsonl` (default `/tmp/lcbench`), and
`compare.py` prints the table at the end.

For one stack, `stack.sh start`, then `run-suite.sh [phase...]`, then
`stack.sh down`.

## Phases

| Phase | Measures |
| --- | --- |
| `deploy` | `lazycloud deploy` of a changed source, five runs |
| `remote` | cold and warm `.remote()`, split into admission, placement, container start and execution |
| `endpoints` | warm and cold endpoint latency, SSE streams, a 1,000-request burst, per-request callbacks |
| `maps` | `spawn_map` admission and throughput at 200, 2,000 and 10,000 inputs |
| `backlog` | two workspaces queueing 1,000 tasks each with the agent stopped, then start order and fairness once it returns |
| `idle` | platform CPU, memory and PostgreSQL statements per second with the app paused |
| `replicas` | idle cost and a 2,000-input map with one and two schedulers |
| `history` | idle cost and a map after growing finished tasks to 100,000 |

The default runs every phase but `replicas` and `history`. `scans.py` shows
which statements and sequential scans a stack runs over a window.

## Reading results

On a shared host, other work moves the numbers. Repeat each comparison, swap
the order of the two commits, and trust differences that hold across runs.
