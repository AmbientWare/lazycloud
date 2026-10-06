#!/bin/sh
# Writes the trace of the slowest container start Jaeger holds, as OTLP
# JSON, to a file.
# Usage: deploy/local/export-trace.sh <jaeger-url> <service> <out.json>
set -eu
jaeger=$1 service=$2 out=$3
min=$(date -u -d '-6 hour' +%Y-%m-%dT%H:%M:%SZ)
max=$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)
id=$(curl -fsS "$jaeger/api/v3/traces?query.service_name=$service&query.operation_name=agent.start&query.start_time_min=$min&query.start_time_max=$max&query.num_traces=500" |
  python3 -c '
import json, sys
starts = [
    (int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"]), s["traceId"])
    for r in json.load(sys.stdin)["result"]["resourceSpans"]
    for ss in r["scopeSpans"]
    for s in ss["spans"]
    if s["name"] == "agent.start"
]
print(max(starts)[1])
')
curl -fsS "$jaeger/api/v3/traces/$id" >"$out"
echo "trace $id written to $out"
