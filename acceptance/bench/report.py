"""Print markdown comparison rows from results/ref.jsonl and results/new.jsonl.

    python report.py [state dir]

The latest record of each scenario wins.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

state = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/lcbench") / "results"


def load(target: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    path = state / f"{target}.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            d = json.loads(line)
            # The backlog phase's one-call warm-up also records "remote".
            if d["scenario"] == "remote" and d["cold"].get("n", 0) < out.get("remote", {}).get(
                "cold", {}
            ).get("n", 0):
                continue
            out[d["scenario"]] = d
    return out


ref, new = load("ref"), load("new")


def d(x: dict | None) -> str:
    if not x or not x.get("n"):
        return "-"
    return f"{x['p50']:g} / {x['p95']:g} / {x['p99']:g}"


def row(label: str, f) -> None:
    def cell(src: dict) -> str:
        try:
            return str(f(src))
        except (KeyError, TypeError, IndexError):
            return "-"

    print(f"| {label} | {cell(ref)} | {cell(new)} |")


print("| Measure | Reference | Rewrite |\n| --- | --- | --- |")
row("deploy, cached image, ms p50/p95/p99", lambda s: d(s["deploy"]["deploy"]))
row("cold .remote() ms", lambda s: d(s["remote"]["cold"]))
for key in ("admit_to_demand_ms", "placement_ms", "container_ready_ms", "execution_ms"):
    row(
        f"cold phase {key} (median of runs)",
        lambda s, k=key: sorted(p[k] for p in s["remote"]["cold_phases"])[
            len(s["remote"]["cold_phases"]) // 2
        ],
    )
row("warm .remote() ms", lambda s: d(s["remote"]["warm"]))
row(
    "warm queue-to-start ms (median)",
    lambda s: sorted(p["warm_queue_to_start_ms_p50"] for p in s["remote"]["cold_phases"])[
        len(s["remote"]["cold_phases"]) // 2
    ],
)
for n in (200, 2000, 10000):
    k = f"map-{n}"
    row(
        f"map {n}: admission/s, total s, results/s, failures",
        lambda s, k=k: (
            f"{s[k]['admission_per_s']}, {s[k]['total_s']}, {s[k]['throughput_per_s']}, {s[k]['failures']}"
        ),
    )
    row(
        f"map {n}: queue wait ms, server span s, containers",
        lambda s, k=k: (
            f"{d(s[k]['server']['queue_wait'])}, {s[k]['server']['server_span_s']}, {s[k]['server']['containers']}"
        ),
    )
    row(
        f"map {n}: platform CPU %, PG exec ms/s, PG stmts/s, PG bytes/s",
        lambda s, k=k: (
            f"{s[k]['cost']['platform_cpu_pct']}, {s[k]['cost']['pg_per_s']['exec_ms']}, "
            f"{s[k]['cost']['pg_per_s']['statements']}, "
            f"{int(s[k]['cost']['pg_per_s']['net_rx'] + s[k]['cost']['pg_per_s']['net_tx'])}"
        ),
    )
row(
    "backlog admission/s per workspace, no capacity",
    lambda s: ", ".join(str(s[f"backlog-submit-{t}"]["admission_per_s"]) for t in "ab"),
)
row(
    "backlog first start after release s, drain s",
    lambda s: f"{s['backlog']['first_start_after_release_s']}, {s['backlog']['drain_s']}",
)
row("backlog start rate/s", lambda s: s["backlog"]["start_rate_per_s"])
row("backlog age at start s", lambda s: d(s["backlog"]["backlog_age_at_start"]))
row("first-quarter share per workspace", lambda s: s["backlog"]["first_quarter_share"])
row("endpoint warm ms", lambda s: d(s["endpoint-warm"]["latency"]))
row(
    "endpoint cold ms",
    lambda s: d(s["endpoint-cold"]["latency"]) + f" ({s['endpoint-cold']['failures']} failed)",
)
row("SSE first event ms (x1)", lambda s: d(s["endpoint-sse-x1"]["first_event"]))
row("SSE inter-event ms (x1, sent every 50)", lambda s: d(s["endpoint-sse-x1"]["inter_event"]))
row(
    "SSE total ms (x20 parallel, ideal 1000)",
    lambda s: d(s["endpoint-sse-x20"]["total"]) + f" ({s['endpoint-sse-x20']['failures']} failed)",
)
for i in range(3):
    row(
        f"1000 concurrent, round {i + 1}: wall s, latency ms, codes",
        lambda s, i=i: (
            f"{s['endpoint-burst']['rounds'][i]['wall_s']}, {d(s['endpoint-burst']['rounds'][i]['latency'])}, "
            f"{s['endpoint-burst']['rounds'][i]['codes']}"
        ),
    )
for label in ("default", "schedulers-1", "schedulers-2", "history-100k"):
    k = f"idle-{label}"
    row(
        f"idle {label}: CPU %, mem MiB, PG stmts/s, PG xact/s, PG bytes/s, Redis cmds/s",
        lambda s, k=k: (
            f"{s[k]['platform_cpu_pct']}, {s[k]['platform_mem_mib']}, {s[k]['pg_per_s']['statements']}, "
            f"{s[k]['pg_per_s']['transactions']}, {int(s[k]['pg_per_s']['net_rx'] + s[k]['pg_per_s']['net_tx'])}, "
            f"{s[k]['redis_per_s'].get('commands', 0)}"
        ),
    )
    row(
        f"idle {label}: PG rows scanned/s (tup_returned)",
        lambda s, k=k: s[k]["pg_per_s"]["tup_returned"],
    )
for label in ("schedulers-2", "history-100k"):
    k = f"map-2000-{label}"
    row(
        f"map 2000 {label}: total s, server span s, platform CPU %, PG exec ms/s",
        lambda s, k=k: (
            f"{s[k]['total_s']}, {s[k]['server']['server_span_s']}, {s[k]['cost']['platform_cpu_pct']}, "
            f"{s[k]['cost']['pg_per_s']['exec_ms']}"
        ),
    )
print()
for name, src in (("Reference", ref), ("Rewrite", new)):
    idle = src.get("idle-default")
    if idle:
        print(
            f"{name} idle processes: "
            + ", ".join(
                f"{p} {v['cpu_pct']}%/{v['mem_mib']:.0f}MiB"
                for p, v in sorted(idle["processes"].items())
            )
        )
