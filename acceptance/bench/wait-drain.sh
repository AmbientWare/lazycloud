#!/bin/sh
# Waits until a stack has no pending, queued or running tasks.
# Usage: wait-drain.sh ref|new
pg=lcbench-$1-postgres-1
while :; do
  left=$(docker exec "$pg" psql -U lazycloud -d lazycloud -At -c \
    "select count(*) from tasks where status in ('pending', 'queued', 'running')")
  [ "$left" = 0 ] && { echo drained; exit 0; }
  echo "$left left"
  sleep 15
done
