"""Before/after rows for one platform from two result files.

    python compare.py <before.jsonl> <after.jsonl>
"""

from __future__ import annotations

import json
import sys


def load(path: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in open(path):
        d = json.loads(line)
        if d["scenario"] == "remote" and d["cold"].get("n", 0) < out.get("remote", {}).get("cold", {}).get("n", 0):
            continue
        out[d["scenario"]] = d
    return out


def median(values: list[float]) -> float:
    s = sorted(values)
    return s[len(s) // 2] if s else float("nan")


def rows(r: dict[str, dict]) -> dict[str, str]:
    out = {}
    if "remote" in r:
        phases = r["remote"]["cold_phases"]
        out["cold .remote() p50 / p95 ms"] = f"{r['remote']['cold']['p50']:g} / {r['remote']['cold']['p95']:g}"
        out["cold assigned-to-ready ms (median)"] = f"{median([p['container_ready_ms'] for p in phases]):g}"
        out["warm .remote() p50 ms"] = f"{r['remote']['warm']['p50']:g}"
    for name in ("map-200", "map-2000", "map-10000", "map-2000-schedulers-2"):
        if name not in r:
            continue
        m = r[name]
        seconds, n = m["cost"]["seconds"], m["inputs"]
        pg = m["process_cpu_pct"].get("postgres", 0)
        out[f"{name}: total s / server span s"] = f"{m['total_s']} / {m['server']['server_span_s']}"
        out[f"{name}: PG CPU ms per task / blocks per task"] = (
            f"{pg * seconds * 10 / n:.2f} / {m['cost']['pg_per_s']['blocks'] * seconds / n:.0f}"
        )
    for name in ("idle-schedulers-1", "idle-schedulers-2"):
        if name in r:
            i = r[name]
            out[f"{name}: statements/s, transactions/s, PG bytes/s"] = (
                f"{i['pg_per_s']['statements']}, {i['pg_per_s']['transactions']}, "
                f"{int(i['pg_per_s']['net_rx'] + i['pg_per_s']['net_tx'])}"
            )
    return out


def main() -> None:
    before, after = rows(load(sys.argv[1])), rows(load(sys.argv[2]))
    print("| Measure | Before | After |\n| --- | --- | --- |")
    for key in before | after:
        print(f"| {key} | {before.get(key, '-')} | {after.get(key, '-')} |")


if __name__ == "__main__":
    main()
