"""Resource counters for one benchmark stack, read from the host.

A snapshot holds cumulative counters: CPU seconds and memory per platform
process (Compose containers via cgroup v2, host processes via /proc), PostgreSQL
statement/transaction/row counters and its container's network bytes. Two
snapshots give rates.

    python sample.py [seconds]   prints the idle cost over the window
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

TICK = os.sysconf("SC_CLK_TCK")
STATE = Path(os.environ.get("LCBENCH_STATE", "/tmp/lcbench/stack"))
PROJECT = "lcbench"
POSTGRES = f"{PROJECT}-postgres-1"
HOST_PROCESSES = ("server", "scheduler", "scheduler2", "agent")


def _run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def _containers(filter_: str) -> list[dict]:
    ids = _run("docker", "ps", "-q", "--no-trunc", "--filter", filter_).split()
    if not ids:
        return []
    # A container that exits between ps and inspect is left out.
    out = subprocess.run(["docker", "inspect", *ids], capture_output=True, text=True).stdout
    return json.loads(out or "[]")


def _host_id() -> str:
    path = STATE / "agent/identity.json"
    return json.loads(path.read_text()).get("host_id", "") if path.exists() else ""


def _cgroup(container_id: str) -> tuple[float, int]:
    base = Path(f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope")
    if not base.exists():
        return 0.0, 0
    usage = 0
    for line in (base / "cpu.stat").read_text().splitlines():
        if line.startswith("usage_usec"):
            usage = int(line.split()[1])
    memory = base / "memory.stat"
    anon = 0
    for line in memory.read_text().splitlines():
        if line.startswith("anon "):
            anon = int(line.split()[1])
    return usage / 1e6, anon


def _proc(pid: int) -> tuple[float, int]:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    cpu = (int(fields[11]) + int(fields[12])) / TICK
    rss = 0
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1]) * 1024
    return cpu, rss


def _net(pid: int) -> tuple[int, int]:
    rx = tx = 0
    for line in Path(f"/proc/{pid}/net/dev").read_text().splitlines()[2:]:
        name, data = line.split(":", 1)
        if name.strip() == "lo":
            continue
        values = data.split()
        rx, tx = rx + int(values[0]), tx + int(values[8])
    return rx, tx


PG_SQL = """
select coalesce(sum(calls),0)::bigint, coalesce(sum(rows),0)::bigint,
       coalesce(sum(shared_blks_hit+shared_blks_read),0)::bigint, coalesce(sum(total_exec_time),0)::float8
  from pg_stat_statements where query not ilike '%pg_stat%';
select xact_commit+xact_rollback, tup_returned, tup_fetched, tup_inserted+tup_updated+tup_deleted
  from pg_stat_database where datname='lazycloud';
"""


@dataclass
class Snapshot:
    at: float
    processes: dict[str, tuple[float, int]] = field(default_factory=dict)
    workloads: tuple[int, float, int] = (0, 0.0, 0)
    pg: dict[str, float] = field(default_factory=dict)


def snapshot() -> Snapshot:
    snap = Snapshot(at=time.time())
    pg_pid = 0
    for c in _containers(f"label=com.docker.compose.project={PROJECT}"):
        service = c["Config"]["Labels"]["com.docker.compose.service"]
        number = c["Config"]["Labels"].get("com.docker.compose.container-number", "1")
        name = service if number == "1" else f"{service}-{number}"
        snap.processes[name] = _cgroup(c["Id"])
        if service == "postgres":
            pg_pid = c["State"]["Pid"]
    for name in HOST_PROCESSES:
        pidfile = STATE / f"{name}.pid"
        if pidfile.exists():
            try:
                snap.processes[name] = _proc(int(pidfile.read_text()))
            except FileNotFoundError:
                pass
    workloads = _containers(f"label=lazycloud.host-id={_host_id()}")
    usage = [_cgroup(c["Id"]) for c in workloads]
    snap.workloads = (len(workloads), sum(u[0] for u in usage), sum(u[1] for u in usage))
    out = _run(
        "docker",
        "exec",
        POSTGRES,
        "psql",
        "-U",
        "lazycloud",
        "-d",
        "lazycloud",
        "-At",
        "-F",
        " ",
        "-c",
        PG_SQL,
    ).split("\n")
    calls, rows, blocks, exec_ms = out[0].split()
    xacts, returned, fetched, written = out[1].split()
    rx, tx = _net(pg_pid)
    snap.pg = {
        "statements": float(calls),
        "rows": float(rows),
        "blocks": float(blocks),
        "exec_ms": float(exec_ms),
        "transactions": float(xacts),
        "tup_returned": float(returned),
        "tup_fetched": float(fetched),
        "tup_written": float(written),
        "net_rx": float(rx),
        "net_tx": float(tx),
    }
    return snap


def delta(a: Snapshot, b: Snapshot) -> dict:
    seconds = b.at - a.at
    processes = {}
    for name, (cpu, mem) in b.processes.items():
        before = a.processes.get(name, (cpu, mem))[0]
        processes[name] = {
            "cpu_pct": round(100 * (cpu - before) / seconds, 2),
            "mem_mib": round(mem / 2**20, 1),
        }
    rate = {k: round((b.pg[k] - a.pg[k]) / seconds, 2) for k in b.pg}
    control = sum(p["cpu_pct"] for p in processes.values())
    return {
        "seconds": round(seconds, 1),
        "platform_cpu_pct": round(control, 2),
        "platform_mem_mib": round(sum(p["mem_mib"] for p in processes.values()), 1),
        "workload_containers": b.workloads[0],
        "workload_cpu_pct": round(100 * (b.workloads[1] - a.workloads[1]) / seconds, 2)
        if a.workloads[0] == b.workloads[0] and b.workloads[1]
        else None,
        "workload_mem_mib": round(b.workloads[2] / 2**20, 1),
        "pg_per_s": rate,
        "processes": processes,
    }


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    first = snapshot()
    time.sleep(seconds)
    print(json.dumps(delta(first, snapshot()), indent=1))


if __name__ == "__main__":
    main()
