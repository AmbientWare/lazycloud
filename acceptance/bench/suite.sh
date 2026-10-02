#!/bin/sh
# Runs run-suite.sh from a frozen copy, so editing the harness mid-run is safe.
# Usage: suite.sh <log file> ref|new [phase...]
set -u
bench=$(cd "$(dirname "$0")" && pwd)
log=$1
shift
copy=$(mktemp /tmp/lcbench-suite.XXXXXX)
cp "$bench/run-suite.sh" "$copy"
LCBENCH_BENCH=$bench sh "$copy" "$@" >"$log" 2>&1
status=$?
echo "exit $status" >>"$log"
rm -f "$copy"
exit $status
