"""Benchmark scenarios, run with one platform's own SDK through sdk.sh.

    sdk.sh ref|new python -m scenarios <scenario> [args]

Each scenario prints one JSON object and appends it to
$LCBENCH_STATE_ROOT/results/<target>.jsonl. Timings come from the client
clock; phase breakdowns come from each platform's own PostgreSQL rows.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import sample

TARGET = os.environ["LCBENCH_TARGET"]
STATE = Path(os.environ.get("LCBENCH_STATE_ROOT", "/tmp/lcbench"))
TOKEN = os.environ["LAZYCLOUD_TOKEN"]
PG = f"{sample.PROJECT[TARGET]}-postgres-1"


def sql(query: str) -> list[list[str]]:
    out = subprocess.run(
        [
            "docker",
            "exec",
            PG,
            "psql",
            "-U",
            "lazycloud",
            "-d",
            "lazycloud",
            "-At",
            "-F",
            "\t",
            "-c",
            query,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def dist(values: list[float], scale: float = 1000.0) -> dict:
    """p50/p95/p99/max in milliseconds (scale) of a list of seconds."""
    if not values:
        return {"n": 0}
    s = sorted(values)

    def q(p: float) -> float:
        return round(s[min(len(s) - 1, int(p * len(s)))] * scale, 1)

    return {
        "n": len(s),
        "p50": q(0.50),
        "p95": q(0.95),
        "p99": q(0.99),
        "max": round(s[-1] * scale, 1),
        "mean": round(statistics.fmean(s) * scale, 1),
    }


def record(name: str, result: dict) -> None:
    result = {
        "target": TARGET,
        "scenario": name,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **result,
    }
    (STATE / "results").mkdir(parents=True, exist_ok=True)
    with (STATE / "results" / f"{TARGET}.jsonl").open("a") as f:
        f.write(json.dumps(result) + "\n")
    print(json.dumps(result, indent=1))


def epoch(column: str) -> str:
    return f"extract(epoch from {column})"


# Per-task timeline columns: created, container created, assigned, ready,
# attempt started, attempt finished, task finished.
if TARGET == "ref":
    TASKS = f"""select t.id, {epoch("t.created_at")}, {epoch("c.created_at")}, {epoch("c.scheduling_assigned_at")},
        {epoch("c.workload_ready_at")}, {epoch("a.started_at")}, {epoch("a.finished_at")}, {epoch("t.finished_at")}, t.status, t.workspace_id, c.id
      from tasks t left join task_attempts a on a.task_id = t.id and a.attempt_number = t.attempt_number
      left join containers c on c.id = a.container_id"""
    ACTIVE_CONTAINERS = """select count(*) from containers c join stubs s on s.id = c.stub_id
      where s.name = '{name}' and c.status in ('pending', 'running')"""
else:
    TASKS = f"""select t.id, {epoch("t.created_at")}, {epoch("c.created_at")}, {epoch("c.assigned_at")},
        {epoch("c.ready_at")}, {epoch("a.started_at")}, {epoch("a.finished_at")}, {epoch("t.finished_at")}, t.status, t.workspace_id, c.id
      from tasks t left join attempts a on a.id = t.current_attempt_id
      left join containers c on c.id = a.container_id"""
    ACTIVE_CONTAINERS = """select count(*) from containers c join releases r on r.id = c.release_id
      join workloads w on w.id = r.workload_id where w.name = '{name}' and c.state <> 'stopped'"""


def tasks_since(t0: float) -> list[list[float | str | None]]:
    rows = sql(f"{TASKS} where t.created_at >= to_timestamp({t0}) order by t.created_at")
    return [
        [None if v == "" else (float(v) if i in range(1, 8) else v) for i, v in enumerate(r)]
        for r in rows
    ]


def phases(row: list) -> dict:
    _, created, c_created, assigned, ready, started, finished, done = row[:8]

    def gap(a: float | None, b: float | None) -> float | None:
        return round((b - a) * 1000, 1) if a is not None and b is not None else None

    return {
        "admit_to_demand_ms": gap(created, c_created)
        if c_created and c_created >= created
        else None,
        "placement_ms": gap(c_created, assigned) if c_created and c_created >= created else None,
        "container_ready_ms": gap(assigned, ready) if c_created and c_created >= created else None,
        "queue_to_start_ms": gap(created, started),
        "execution_ms": gap(started, finished),
        "result_commit_ms": gap(finished, done),
    }


def task_summary(rows: list) -> dict:
    done = [r for r in rows if r[7] is not None]
    if not rows:
        return {}
    first, last = min(r[1] for r in rows), max((r[7] for r in done), default=None)
    starts = sorted(r[5] for r in rows if r[5] is not None)
    return {
        "tasks": len(rows),
        "statuses": {s: sum(1 for r in rows if r[8] == s) for s in {r[8] for r in rows}},
        "containers": len({r[10] for r in rows if r[10]}),
        "queue_wait": dist([r[5] - r[1] for r in rows if r[5] is not None]),
        "end_to_end": dist([r[7] - r[1] for r in done]),
        "execution": dist([r[6] - r[5] for r in rows if r[5] is not None and r[6] is not None]),
        "server_span_s": round(last - first, 2) if last else None,
        "start_span_s": round(starts[-1] - starts[0], 2) if len(starts) > 1 else None,
    }


# --- 1. deploy ---------------------------------------------------------------


def deploy(runs: str = "5") -> None:
    times, failures, urls = [], 0, {}
    for i in range(int(runs)):
        # A source change forces a new release on each run; the image is cached.
        Path("nonce.py").write_text(f"NONCE = {time.time_ns()}\n")
        t = time.perf_counter()
        out = subprocess.run(["lazycloud", "deploy", "benchapp.py"], capture_output=True, text=True)
        elapsed = time.perf_counter() - t
        if out.returncode:
            failures += 1
            print(out.stdout[-2000:], out.stderr[-2000:], file=sys.stderr)
            continue
        times.append(elapsed)
        for url in re.findall(r"http://[a-z0-9-]+\.lazycloud\.localhost:\d+[^\s,│]*", out.stdout):
            urls[url.split("//")[1].split("-")[0]] = url
    (STATE / f"{TARGET}-urls.json").write_text(json.dumps(urls))
    record("deploy", {"deploy": dist(times), "failures": failures, "urls": urls})


# --- 2. remote ---------------------------------------------------------------

COLD_CHILD = """
import json, time
from benchapp import hold
t = time.perf_counter(); v = hold.remote(1); cold = time.perf_counter() - t
warm = []
for i in range(int({warm})):
    t = time.perf_counter(); hold.remote(i); warm.append(time.perf_counter() - t)
print("LCBENCH" + json.dumps({{"cold": cold, "warm": warm, "ok": v == 1}}))
"""


def remote(runs: str = "5", warm: str = "30") -> None:
    cold, warm_all, phase_rows, failures = [], [], [], 0
    for _ in range(int(runs)):
        # New source makes a new release, so the first call needs a new container.
        Path("nonce.py").write_text(f"NONCE = {time.time_ns()}\n")
        t0 = time.time()
        out = subprocess.run(
            [sys.executable, "-c", COLD_CHILD.format(warm=warm)], capture_output=True, text=True
        )
        line = next((ln for ln in out.stdout.splitlines() if ln.startswith("LCBENCH")), None)
        if out.returncode or line is None:
            failures += 1
            print(out.stdout[-1500:], out.stderr[-1500:], file=sys.stderr)
            continue
        data = json.loads(line[7:])
        cold.append(data["cold"])
        warm_all.extend(data["warm"])
        rows = tasks_since(t0)
        if rows:
            phase_rows.append(phases(rows[0]))
            warm_phases = [phases(r) for r in rows[1:]]
            phase_rows[-1]["warm_queue_to_start_ms_p50"] = statistics.median(
                [p["queue_to_start_ms"] for p in warm_phases if p["queue_to_start_ms"] is not None]
                or [0]
            )
    record(
        "remote",
        {
            "cold": dist(cold),
            "warm": dist(warm_all),
            "cold_phases": phase_rows,
            "failures": failures,
        },
    )


# --- 3/4. spawn_map admission and map throughput ------------------------------


def _sampled(fn):
    """Run fn while sampling platform cost; return (fn result, cost delta)."""
    before = sample.snapshot(TARGET)
    value = fn()
    return value, sample.delta(before, sample.snapshot(TARGET))


def map_(n: str, label: str = "") -> None:
    import benchapp

    fn = benchapp.echo
    count = int(n)
    if label.startswith("cold"):
        # New source makes a new release, so every container starts cold.
        Path("nonce.py").write_text(f"NONCE = {time.time_ns()}\n")
    t0 = time.time()

    def run() -> tuple[float, float, list]:
        start = time.perf_counter()
        calls = fn.spawn_map([(i,) for i in range(count)])
        submitted = time.perf_counter() - start
        values = []
        for call in calls:
            try:
                values.append(call.get())
            except Exception:
                values.append(None)
        return submitted, time.perf_counter() - start, values

    (submitted, total, values), cost = _sampled(run)
    wrong = sum(1 for i, v in enumerate(values) if v != i)
    rows = tasks_since(t0)
    record(
        f"map-{count}" + (f"-{label}" if label else ""),
        {
            "inputs": count,
            "admission_s": round(submitted, 2),
            "admission_per_s": round(count / submitted, 1),
            "total_s": round(total, 2),
            "throughput_per_s": round(count / total, 1),
            "failures": wrong,
            "server": task_summary(rows),
            "cost": {k: v for k, v in cost.items() if k != "processes"},
            "process_cpu_pct": {k: v["cpu_pct"] for k, v in cost["processes"].items()},
        },
    )


def map_server(t0: str, n: str, note: str) -> None:
    """Server-side record of a map whose client did not finish (see note)."""
    rows = tasks_since(float(t0))
    record(
        f"map-{n}",
        {
            "inputs": int(n),
            "client_incomplete": note,
            "server": task_summary(rows),
            "admission_per_s": round(
                len(rows) / max(1e-3, max(r[1] for r in rows) - min(r[1] for r in rows)), 1
            ),
        },
    )


def backlog(n: str, tag: str) -> None:
    """Submit n tasks to `hold` in this process's workspace, then wait for capacity.

    The caller stops the host first, runs one of these per workspace at once,
    and writes STATE/backlog-release (its content the release epoch) once every
    STATE/backlog-submitted-<tag> exists.
    """
    import benchapp

    count = int(n)
    start = time.perf_counter()
    calls = benchapp.hold.spawn_map([(i,) for i in range(count)])
    admission = time.perf_counter() - start
    (STATE / f"backlog-submitted-{tag}").write_text(str(admission))
    while not (STATE / "backlog-release").exists():
        time.sleep(0.5)
    failures = 0
    for call in calls:
        try:
            call.get()
        except Exception:
            failures += 1
    record(
        f"backlog-submit-{tag}",
        {
            "inputs": count,
            "admission_s": round(admission, 2),
            "admission_per_s": round(count / admission, 1),
            "failures": failures,
        },
    )


def submit(n: str) -> None:
    """Queue n `hold` tasks and exit without waiting, for EXPLAIN at a backlog."""
    import benchapp

    start = time.perf_counter()
    calls = benchapp.hold.spawn_map([(i,) for i in range(int(n))])
    print(json.dumps({"submitted": len(calls), "seconds": round(time.perf_counter() - start, 2)}))


def fairness(t0: str, released: str) -> None:
    """Backlog age and workspace interleaving of the tasks created since t0."""
    rows = tasks_since(float(t0))
    release = float(released)
    starts = sorted((r[5], r[9]) for r in rows if r[5] is not None)
    by_ws = sorted({r[9] for r in rows})
    quarter = max(1, len(starts) // 4)
    record(
        "backlog",
        {
            "tasks": len(rows),
            "workspaces": len(by_ws),
            "statuses": {s: sum(1 for r in rows if r[8] == s) for s in {r[8] for r in rows}},
            "first_start_after_release_s": round(starts[0][0] - release, 2) if starts else None,
            "drain_s": round(starts[-1][0] - release, 2) if starts else None,
            "start_rate_per_s": round(len(starts) / max(0.001, starts[-1][0] - starts[0][0]), 1)
            if len(starts) > 1
            else None,
            "backlog_age_at_start": dist([r[5] - r[1] for r in rows if r[5] is not None], 1.0),
            "first_quarter_share": [
                round(sum(1 for _, w in starts[:quarter] if w == ws) / quarter, 3) for ws in by_ws
            ],
            "median_start_after_release_s": [
                round(statistics.median([s for s, w in starts if w == ws]) - release, 2)
                for ws in by_ws
            ],
        },
    )


# --- 5. endpoints --------------------------------------------------------------


def _urls() -> dict:
    return json.loads((STATE / f"{TARGET}-urls.json").read_text())


HEADERS = {"Authorization": f"Bearer {TOKEN}", "content-type": "application/json"}


def endpoint_warm(n: str = "500") -> None:
    url = _urls()["ping"]
    lat, failures = [], 0
    with httpx.Client(timeout=200, headers=HEADERS) as client:
        for i in range(20):
            client.post(url, json={"n": i})
        for i in range(int(n)):
            t = time.perf_counter()
            r = client.post(url, json={"n": i})
            lat.append(time.perf_counter() - t)
            failures += r.status_code != 200 or r.json().get("n") != i
    record("endpoint-warm", {"latency": dist(lat), "failures": failures})


def endpoint_cold(runs: str = "5") -> None:
    url = _urls()["coldping"]
    lat, waits, failures = [], [], 0
    for i in range(int(runs)):
        t = time.perf_counter()
        while int(sql(ACTIVE_CONTAINERS.format(name="coldping"))[0][0]) > 0:
            if time.perf_counter() - t > 600:
                raise RuntimeError("coldping containers never retired")
            time.sleep(0.5)
        waits.append(time.perf_counter() - t)
        # Fresh connection so no pooled socket hides the cold path.
        with httpx.Client(timeout=300, headers=HEADERS) as fresh:
            t = time.perf_counter()
            r = fresh.post(url, json={"n": i})
            lat.append(time.perf_counter() - t)
        failures += r.status_code != 200
    record(
        "endpoint-cold",
        {"latency": dist(lat), "retire_wait_s": dist(waits, 1.0), "failures": failures},
    )


def endpoint_sse(n: str = "50", parallel: str = "1") -> None:
    url = _urls()["stream"]
    first, total, gaps, failures = [], [], [], 0

    async def one(client: httpx.AsyncClient) -> None:
        nonlocal failures
        t = time.perf_counter()
        seen, last = 0, None
        try:
            async with client.stream("GET", url) as r:
                async for chunk in r.aiter_raw():
                    now = time.perf_counter()
                    k = chunk.count(b"data:")
                    if k and seen == 0:
                        first.append(now - t)
                    elif k and last is not None:
                        gaps.append(now - last)
                    if k:
                        last = now
                    seen += k
            total.append(time.perf_counter() - t)
            failures += seen != 20 or r.status_code != 200
        except httpx.HTTPError:
            failures += 1

    async def main() -> None:
        async with httpx.AsyncClient(
            timeout=200, headers=HEADERS, limits=httpx.Limits(max_connections=int(parallel) + 10)
        ) as client:
            await one(client)
            first.clear()
            total.clear()
            gaps.clear()
            for _ in range(int(n) // int(parallel)):
                await asyncio.gather(*(one(client) for _ in range(int(parallel))))

    asyncio.run(main())
    record(
        f"endpoint-sse-x{parallel}",
        {
            "first_event": dist(first),
            "total": dist(total),
            "inter_event": dist(gaps),
            "failures": failures,
            "expected_total_ms": 20 * 50,
        },
    )


def endpoint_burst(n: str = "1000", rounds: str = "3") -> None:
    url = _urls()["ping"]
    results = []

    async def main() -> None:
        limits = httpx.Limits(max_connections=int(n) + 10, max_keepalive_connections=int(n) + 10)
        async with httpx.AsyncClient(timeout=300, headers=HEADERS, limits=limits) as client:
            for _ in range(int(rounds)):
                lat: list[float] = []
                codes: dict[str, int] = {}

                async def one(
                    i: int, lat: list[float] = lat, codes: dict[str, int] = codes
                ) -> None:
                    t = time.perf_counter()
                    try:
                        r = await client.post(url, json={"n": i})
                        code = str(r.status_code)
                    except httpx.HTTPError as exc:
                        code = type(exc).__name__
                    lat.append(time.perf_counter() - t)
                    codes[code] = codes.get(code, 0) + 1

                t = time.perf_counter()
                await asyncio.gather(*(one(i) for i in range(int(n))))
                results.append(
                    {
                        "wall_s": round(time.perf_counter() - t, 2),
                        "latency": dist(lat),
                        "codes": codes,
                    }
                )

    asyncio.run(main())
    record("endpoint-burst", {"concurrency": int(n), "rounds": results})


CALLBACK_PORT = 29999


def endpoint_callback(n: str = "1000", concurrency: str = "100", wait: str = "180") -> None:
    """Requests to an endpoint with callback_url, and the callback each one owes.

    A local receiver records when each request's callback arrives, keyed by
    the task_id the platform sends, which is the request's X-Request-Id.
    """
    import http.server
    import socketserver

    url = _urls()["pingcb"]
    arrived: dict[str, list[float]] = {}
    lock = threading.Lock()

    class Hook(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            now = time.time()
            try:
                task = str(json.loads(body).get("task_id", ""))
            except ValueError:
                task = ""
            with lock:
                arrived.setdefault(task, []).append(now)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_: object) -> None:
            return

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        # Deliveries arrive in parallel; a short backlog drops SYNs and adds
        # a one-second retransmit to the measured delay.
        request_queue_size = 1024

    server = Server(("0.0.0.0", CALLBACK_PORT), Hook)  # noqa: S104
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ended: dict[str, float] = {}
    began: dict[str, float] = {}
    ends: list[float] = []
    starts: list[float] = []
    lat: list[float] = []
    codes: dict[str, int] = {}

    def arrivals() -> int:
        with lock:
            return sum(len(v) for v in arrived.values())

    def settle(want: int, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline and arrivals() < want:
            time.sleep(0.1)

    async def main() -> None:
        limits = httpx.Limits(max_connections=int(concurrency) + 10, max_keepalive_connections=int(concurrency) + 10)
        gate = asyncio.Semaphore(int(concurrency))
        async with httpx.AsyncClient(timeout=300, headers=HEADERS, limits=limits) as client:

            async def one(i: int, timed: bool) -> None:
                async with gate:
                    t, wall_start = time.perf_counter(), time.time()
                    try:
                        r = await client.post(url, json={"n": i})
                        code = str(r.status_code)
                        request = r.headers.get("x-request-id", "")
                    except httpx.HTTPError as exc:
                        code, request = type(exc).__name__, ""
                    if timed:
                        lat.append(time.perf_counter() - t)
                        codes[code] = codes.get(code, 0) + 1
                        ends.append(time.time())
                        starts.append(wall_start)
                        if request:
                            ended[request], began[request] = ends[-1], wall_start

            # Warm up, and let the warm-up's callbacks land before timing.
            await asyncio.gather(*(one(i, False) for i in range(20)))
            await asyncio.to_thread(settle, 20, 60)
            with lock:
                arrived.clear()
            start = time.perf_counter()
            await asyncio.gather(*(one(i, True) for i in range(int(n))))
            wall.append(time.perf_counter() - start)

    wall: list[float] = []
    asyncio.run(main())
    settle(int(n), float(wait))
    server.shutdown()
    with lock:
        if ended:
            delays = [arrived[r][0] - t for r, t in ended.items() if r in arrived]
            totals = [arrived[r][0] - t for r, t in began.items() if r in arrived]
            missing = sum(1 for r in ended if r not in arrived)
            duplicates = sum(len(v) - 1 for r, v in arrived.items() if r in ended)
            matched = "request id"
        else:
            # Without a request id on the response, pair the n-th response to
            # end with the n-th callback to arrive.
            firsts = sorted(v[0] for v in arrived.values())
            delays = [a - e for a, e in zip(firsts, sorted(ends), strict=False)]
            totals = [a - b for a, b in zip(firsts, sorted(starts), strict=False)]
            missing = max(0, len(ends) - len(firsts))
            duplicates = sum(len(v) - 1 for v in arrived.values())
            matched = "arrival order"
    record("endpoint-callback", {
        "requests": int(n), "concurrency": int(concurrency), "wall_s": round(wall[0], 2),
        "latency": dist(lat), "codes": codes, "matched_by": matched,
        "callback_delay": dist(delays), "callback_since_request": dist(totals), "callbacks_missing": missing, "callbacks_duplicated": duplicates,
    })


def idle(seconds: str = "120", label: str = "default") -> None:
    before = sample.snapshot(TARGET)
    time.sleep(float(seconds))
    record(f"idle-{label}", sample.delta(before, sample.snapshot(TARGET)))


SCENARIOS = {
    "deploy": deploy,
    "remote": remote,
    "map": map_,
    "map-server": map_server,
    "backlog": backlog,
    "fairness": fairness, "submit": submit,
    "endpoint-warm": endpoint_warm,
    "endpoint-cold": endpoint_cold,
    "endpoint-sse": endpoint_sse,
    "endpoint-burst": endpoint_burst, "endpoint-callback": endpoint_callback,
    "idle": idle,
}

if __name__ == "__main__":
    SCENARIOS[sys.argv[1]](*sys.argv[2:])
