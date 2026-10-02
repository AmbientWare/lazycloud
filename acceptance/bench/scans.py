"""Which recurring statements and table scans a stack runs over a window.

    python scans.py ref|new [seconds]

Resets pg_stat_statements, waits, and prints the statements by buffers read
and the tables by rows read through sequential scans, with per-second rates.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import sample

TABLES = """select coalesce(json_object_agg(relname, json_build_array(seq_scan, seq_tup_read, idx_scan, idx_tup_fetch)), '{}')
from pg_stat_user_tables"""
STATEMENTS = """select coalesce(json_agg(s), '[]') from (
  select calls, rows, shared_blks_hit + shared_blks_read as blocks,
         left(regexp_replace(query, '\\s+', ' ', 'g'), 110) as query
  from pg_stat_statements where query not ilike '%pg_stat%'
  order by shared_blks_hit + shared_blks_read desc limit 12) s"""


def psql(target: str, query: str) -> str:
    return subprocess.run(
        ["docker", "exec", f"{sample.PROJECT[target]}-postgres-1", "psql", "-U", "lazycloud", "-d", "lazycloud",
         "-At", "-c", query],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def main() -> None:
    target = sys.argv[1]
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60
    psql(target, "select pg_stat_statements_reset()")
    before = json.loads(psql(target, TABLES))
    time.sleep(seconds)
    after = json.loads(psql(target, TABLES))
    statements = json.loads(psql(target, STATEMENTS))
    calls = psql(target, "select sum(calls) from pg_stat_statements where query not ilike '%pg_stat%'")
    print(f"statements/s {float(calls or 0) / seconds:.2f}")
    for name, now in sorted(after.items(), key=lambda kv: -(kv[1][1] - before.get(kv[0], kv[1])[1])):
        was = before.get(name, now)
        seq_rows = now[1] - was[1]
        if seq_rows:
            print(f"table {name}: seq scans/s {(now[0] - was[0]) / seconds:.2f}, seq rows/s {seq_rows / seconds:.0f}")
    for s in statements:
        print(f"{s['calls']:>6} calls {s['blocks']:>8} blocks  {s['query']}")


if __name__ == "__main__":
    main()
