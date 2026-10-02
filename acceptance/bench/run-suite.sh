#!/bin/sh
# Runs benchmark phases against a started stack (stack.sh start). Results
# append to $LCBENCH_RESULTS.
# Usage: run-suite.sh [phase...]
#   phases: deploy remote endpoints maps backlog idle replicas history
#   default: deploy remote endpoints maps backlog idle
set -eu
bench=$(cd "$(dirname "$0")" && pwd)
root=${LCBENCH_ROOT:-/tmp/lcbench}
state=${LCBENCH_STATE:-$root/stack}
export LCBENCH_ROOT="$root" LCBENCH_STATE="$state"
phases=${*:-deploy remote endpoints maps backlog idle}
run() { "$bench/sdk.sh" python -m scenarios "$@"; }

schedulers() {
  "$bench/stack.sh" scheduler2-stop
  [ "$1" = 1 ] || "$bench/stack.sh" scheduler2
  sleep 20
}

# Idle means a deployed app scaled to zero: pause stops every container.
idle_window() {
  "$bench/sdk.sh" lazycloud app pause lcbench
  quiesce
  sleep 30
  run idle 120 "$1"
  "$bench/sdk.sh" lazycloud app resume lcbench
}

# Waits until no workload container of the stack is running.
quiesce() {
  i=0
  while [ "$(python3 -c "import sample; print(sample.snapshot().workloads[0])" 2>/dev/null)" != 0 ]; do
    i=$((i + 1))
    [ $i -lt 240 ] || { echo "workload containers still running" >&2; return 1; }
    sleep 5
  done
}

cd "$bench"
for phase in $phases; do
  echo "== $phase" >&2
  case $phase in
    deploy) run deploy 5 ;;
    # The first container on a new host also prepares its image; warm up first.
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
      # Build the image in the second workspace and warm nothing there.
      LCBENCH_WORKSPACE=dev2 run remote 1 0
      "$bench/stack.sh" agent-stop
      sleep 20
      rm -f "$state"/backlog-*
      t0=$(date +%s.%N)
      run backlog 1000 a >"$state/backlog-a.out" 2>&1 &
      a=$!
      LCBENCH_WORKSPACE=dev2 run backlog 1000 b >"$state/backlog-b.out" 2>&1 &
      b=$!
      while [ ! -f "$state/backlog-submitted-a" ] || [ ! -f "$state/backlog-submitted-b" ]; do sleep 0.5; done
      # Accepted work waits with no capacity before it is released.
      sleep 30
      released=$(date +%s.%N)
      "$bench/stack.sh" agent-start
      echo "$released" >"$state/backlog-release"
      wait $a $b
      run fairness "$t0" "$released" ;;
    idle) idle_window default ;;
    replicas)
      schedulers 1
      idle_window schedulers-1
      schedulers 2
      idle_window schedulers-2
      run map 2000 schedulers-2
      schedulers 1 ;;
    history)
      python3 "$bench/history.py" 100000
      idle_window history-100k
      run map 2000 history-100k ;;
    *) echo "unknown phase $phase" >&2; exit 2 ;;
  esac
done
