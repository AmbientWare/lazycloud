#!/bin/sh
# Waits up to $2 seconds (default 600) for the reference worker container $1
# to report healthy and prints how long that took.
name=$1
limit=${2:-600}
start=$(date +%s)
while :; do
  status=$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null)
  now=$(date +%s)
  if [ "$status" = healthy ]; then echo "healthy after $((now - start))s"; exit 0; fi
  if [ $((now - start)) -ge "$limit" ]; then echo "still $status after ${limit}s"; exit 1; fi
  sleep 5
done
