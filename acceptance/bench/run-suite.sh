#!/bin/sh
# Runs the benchmark scenarios against one stack. Results append to
# $LCBENCH_STATE_ROOT/results/<target>.jsonl.
# Usage: run-suite.sh ref|new [phase...]
#   phases: deploy remote endpoints maps backlog idle replicas history
set -eu
bench=${LCBENCH_BENCH:-$(cd "$(dirname "$0")" && pwd)}
state=${LCBENCH_STATE_ROOT:-/tmp/lcbench}
target=$1
shift
phases=${*:-deploy remote endpoints maps backlog idle}
run() { "$bench/sdk.sh" "$target" python -m scenarios "$@"; }
second_workspace() { if [ "$target" = ref ]; then echo tenant-b; else echo dev2; fi; }

agent_stop() {
  if [ "$target" = ref ]; then
    (cd "${LCBENCH_REF:-/tmp/lc-ref}" && docker compose stop agent)
  else
    "$bench/rewrite-stack.sh" agent-stop
  fi
}

agent_start() {
  if [ "$target" = ref ]; then
    (cd "${LCBENCH_REF:-/tmp/lc-ref}" && docker compose start agent)
  else
    "$bench/rewrite-stack.sh" agent-start
  fi
}

# Sets the scheduler replica count (the reference also scales its fleet controller).
schedulers() {
  if [ "$target" = ref ]; then
    (cd "${LCBENCH_REF:-/tmp/lc-ref}" && docker compose up -d --no-deps --scale scheduler="$1" \
      --scale fleet-controller="$1" scheduler fleet-controller)
  else
    "$bench/rewrite-stack.sh" scheduler-stop2
    [ "$1" = 1 ] || "$bench/rewrite-stack.sh" scheduler2
  fi
  sleep 20
}

idle_window() {
  "$bench/sdk.sh" "$target" lazycloud app pause lcbench
  quiesce
  sleep 30
  run idle 120 "$1"
  "$bench/sdk.sh" "$target" lazycloud app resume lcbench
}

# Waits until no workload container of the stack is running.
quiesce() {
  i=0
  while [ "$(python3 -c "import sample; print(sample.snapshot('$target').workloads[0])" 2>/dev/null)" != 0 ]; do
    i=$((i + 1))
    [ $i -lt 240 ] || { echo "workload containers still running" >&2; return 1; }
    sleep 5
  done
}

cd "$bench"
for phase in $phases; do
  echo "== $target $phase" >&2
  case $phase in
    deploy) run deploy 5 ;;
    # The first container on a new host also prepares its image; the
    # reference was measured with its image built, so warm up first.
    remote) run remote 1 0 >/dev/null; run remote 5 30 ;;
    endpoints)
      run endpoint-warm 1000
      run endpoint-sse 50 1
      run endpoint-sse 200 20
      run endpoint-burst 1000 3
      run endpoint-cold 5
      run endpoint-callback 1000 100 ;;
    maps)
      run map 200
      run map 2000
      run map 10000 ;;
    backlog)
      # The reference serves the second workspace only once the machine lists it.
      if [ "$target" = ref ]; then
        "$bench/sdk.sh" ref lazycloud machine update lcbench-agent --workspaces tenant-customer,tenant-b
      fi
      # Build the image and warm nothing in the second workspace first.
      LCBENCH_WORKSPACE=$(second_workspace) run remote 1 0
      agent_stop
      sleep 20
      rm -f "$state"/backlog-*
      t0=$(date +%s.%N)
      run backlog 1000 a >"$state/backlog-a.out" 2>&1 &
      a=$!
      LCBENCH_WORKSPACE=$(second_workspace) run backlog 1000 b >"$state/backlog-b.out" 2>&1 &
      b=$!
      while [ ! -f "$state/backlog-submitted-a" ] || [ ! -f "$state/backlog-submitted-b" ]; do sleep 0.5; done
      # Accepted work waits with no capacity before it is released.
      sleep 30
      released=$(date +%s.%N)
      agent_start
      echo "$released" >"$state/backlog-release"
      wait $a $b
      run fairness "$t0" "$released" ;;
    idle)
      # Idle means a deployed app scaled to zero: pause stops every container.
      idle_window default ;;
    replicas)
      schedulers 1
      idle_window schedulers-1
      schedulers 2
      idle_window schedulers-2
      run map 2000 schedulers-2
      if [ "$target" = new ]; then schedulers 1; fi ;;
    history)
      python3 "$bench/history.py" "$target" 100000
      idle_window history-100k
      run map 2000 history-100k ;;
    *) echo "unknown phase $phase" >&2; exit 2 ;;
  esac
done
