# Start latency

## Scope

Measure first, then fix what the numbers show. Seen on 2026-10-06:

- Container create took 2.2 s on a freshly launched c7i host against about
  0.3 s on warm hosts.
- The python cold start trails Beam by about 0.2 s, mostly the ~1 s
  runtime stage.
- During the benchmark the fleet launched four or more new hosts in about
  25 minutes and dropped earlier ones, so two of three torch cold starts
  landed on hosts that had never seen the image. Find why (the warm
  floor, reserve churn, Spot) and whether placement prefers hosts that
  already hold an image's layers.

Owns a scratch investigation first; fixes go to the owning packages with
the integrator's go-ahead.

## Evidence to record

- A timeline per finding from the agent and snapshotter logs or traces.
- Before and after for each fix.

## Progress

Measured 2026-10-06; no product code changed. Sources:

- X-Ray traces of 06:40-07:25 UTC (us-east-1): 21 `agent.start` traces
  with Docker and snapshotter spans.
- CloudTrail EC2 events in all four regions.
- Two scratch hosts from the CPU node image `ami-0104d7b9500423a78`
  (c7i.2xlarge with hibernation, m7i.large) in a scratch us-east-2 VPC,
  with the snapshotter from this branch's agent bundle, plain
  `python:3.12-slim` under runsc, and the agent's mounts, limits and
  supervisor.

No scheduler log of the window remains. Its pods were replaced at 07:15
and again by 13:18 (new role sessions in CloudTrail), and the account has
no CloudWatch log groups. The fleet decisions below come from CloudTrail
read against `internal/compute`.

### 1. Container create after a resume

Prod, 07:08:05, host `01a10ff9-…-7b69` (i-0a335ed829c4ede2a, c7i.2xlarge
Spot reserve). Bought 06:49:34, hibernated 06:51:38, resumed 06:52:47,
hibernated 06:57:59, resumed 07:03:40. It ran no container before
07:08:05. `agent.create` took 2159 ms: Docker create 611 ms and start
1546 ms. On warm hosts with the image cached, create is 53-69 ms and
start 210-290 ms. The first start of an image on a warm host takes
0.5-0.8 s, because the lazy layer mounts and fills run inside it.

Scratch hosts, plain image (create + start, ms):

| Case | c7i.2xlarge | m7i.large |
| --- | --- | --- |
| First container after boot | 218 + 532 | 227 + 433 |
| Later containers | 61 + 296-307 | 63-65 + 202-216 |
| First after hibernate/resume, `image_size=0` (node image) | 2304 + 1550; again 1090 + 1338 | |
| Same, binaries read first (0.9 s) | 638 + 465 | |
| Same, `swapoff -a; swapon -a` (7.7 s) and binaries read | 399 + 360 | |
| First after resume, default `image_size` (2/5 of RAM) | 75 + 318; 88 + 318 | |

Cause: `deploy/ami/node-setup.sh` sets `/sys/power/image_size` to 0. The
kernel then writes the smallest image: it pushes dockerd's, containerd's
and the snapshotter's memory to the swap file (pswpout 43k pages) and
drops the page cache. After the resume, the first container faults that
memory back in small random reads from EBS (pswpin 24k pages; dockerd
still had 21 MB swapped after it) and reads runsc, the shim and Docker
again. EBS lazy loading is not a factor: files nothing had read came in
at the same speed on the first read as on the second (10.7 MB in 26 ms).

With the default image size, the image grows from about 0.6 GB to
1.1 GB. Thaw comes 8.7 s after StartInstances instead of 7.9 s, and the
hibernate takes 32 s either way.

On a host that never hibernated, the first container ever costs about
0.4 s more than later ones (the first runsc and shim run).

Proposed fixes:

- Remove the `image_size` 0 line from node-setup.sh, so hibernation
  keeps userspace and the page cache in the image. Expected gain: 2-3.5
  s off the first start after each resume, for about 0.8 s more resume
  time. The node image owner bakes it; the fleet is replaced at the next
  Ship.
- Run one throwaway runsc container while a reserve prepares (before
  `ReserveReady`). With the default image size, that warmth survives the
  hibernation. Expected gain: about 0.4 s on the first start a reserve
  serves.

### 2. Runtime stage (supervisor plus runner under gVisor)

Prod `agent.runtime` with the image cached: 0.97-1.1 s for the images
built on `python:3.12-slim` with no build step (17f8…, 8f5d…, 4
layers), and 0.69-0.72 s for 9b10…, which adds a pip layer.

Scratch m7i.large, `--cpus 1`, runtime bind-mounted read-only as the
agent does. Each probe is a fresh `python3 -c …` in a running container,
timed inside the container (first run / second run in the same
container):

| Probe | runsc, slim | runsc, stdlib compiled | runc, slim | runc, compiled |
| --- | --- | --- | --- | --- |
| bare interpreter | 17-22 | 15-22 | 8-9 | 8 |
| `import runner.protocol` | 509-737 / 252-287 | 292-337 / 287-292 | 349 / 143 | 138 / 137 |
| `+ startup_app` (SDK app) | | 366-397 | | 189 |

- Docker start returning to the supervisor's first dial of the link
  socket: 0-11 ms, so the stage is almost all the runner's Python start.
- Docker Hub's `python:*-slim` ships no stdlib bytecode. Every new
  container compiles what the runner imports into its fresh writable
  layer. That costs 220-440 ms under runsc (210 ms under runc). Prod
  confirms it: an image whose build ran pip carries the stdlib bytecode
  pip wrote, and its runtime stage is about 0.3 s shorter.
- Compiled, the runner and an app import in about 370 ms under runsc
  against 190 ms under runc. The bind mount is not the cost: the same
  runtime copied into the image measured 328-337 ms.
- The heaviest imports under runsc are
  `lazycloud._shared.function_payloads` (137 ms cumulative, via
  `runner.protocol` → `function_display`), `pydantic.types` (55 ms) and
  `pydantic._internal._decorators` (28 ms).
- The remaining 0.3-0.4 s of prod's stage was not reproduced here. It
  covers the reads through the lazy FUSE layers, the Configure and Ready
  round trips, and loading the handler.

Proposed fixes:

- Compile the stdlib to bytecode (unchecked-hash) in every image the
  platform builds. That means the platform bases (base-python already
  plans it) and user images on Docker Hub Python bases (one
  `compileall` step in the renderer after the base). Expected gain:
  0.25-0.4 s per cold start, which covers the 0.2 s gap to Beam.
- Import `function_display` and `function_payloads` in the runner on the
  first result instead of at load. Expected gain: up to 0.1 s; needs a
  runner measurement first.

### 3. Fleet churn and image locality

Timeline, us-east-2 unless named (CloudTrail):

- 06:43:14-06:44:29 spin-up. The scheduler resumes three reserves and
  buys the two warm floor hosts (m7i.large Spot and on-demand).
- 06:44:31-32 the operator's root credentials, not the scheduler, cancel
  the Spot requests and terminate all five hosts.
- 06:44:53-56 the planner buys again: warm m7i.large on-demand
  (`3e60`, 2b) and Spot (`405e`, 2c), and the large-shape reserves kept
  apart (c6a.8xlarge on-demand, m6a.8xlarge Spot), stopped at 06:45.
- 06:49:33-34, 5 min later (`floorDue` waits `IdleTimeout`), it buys the
  stopped-floor reserves: c7i.2xlarge Spot (`7b69`, 2c) and c6a.2xlarge
  on-demand in us-east-1b (`7b72`). Both hibernate by 06:51:43.
- 06:52 python starts take the warm hosts' room. `grow` resumes
  `7b72` (us-east-1, 06:52:19; its first start pulled for 8.1 s, one
  7.3 s blob request) and `7b69` (06:52:47).
- 06:57:19 the on-demand floor has been short for 5 min while `7b72`
  serves, so a new c6a.2xlarge reserve is bought (`01a11000`, 2a). At
  06:59:21 `7b72`, now idle and not needed for the floor, drains and is
  terminated. The same pattern repeats at 07:08:41 (c6a.2xlarge Spot).
- 06:58:35 and 07:06:42 placements take warm room, and `grow` buys
  m7i.large on-demand hosts (`c7f3`, `02bd`). `02bd` serves one
  container and terminates at 07:13:16, 5 min after it went idle.
- 07:03:40 `405e` (Spot warm host, the only one holding the torch image
  since 06:56:41) is drained and terminated, and `7b69` resumes in the
  same second. A one-time Spot host cannot stop, so it leaves by
  draining. The log that would name the action is gone.
- 07:13:32-38 Spot ICE for m7i.large in us-east-2c three times; bought
  in us-west-2 instead at 07:13:39.
- 07:14:39-44 root credentials cancel the Spot requests and terminate
  all eight hosts, and new scheduler pods start at 07:15.

So of the churn in the window, two bulk terminations were operator
actions. The rest is the planner working as written:

- Each cold start spends the warm floor's free room (1 CPU, 4 GiB per
  market), so `grow` resumes or buys another host at once.
- Any host beyond the warm target leaves after 5 min idle (`retain`,
  `IdleTimeout`). The benchmark's starts were minutes apart, so hosts
  that had just pulled an image left before the next start.
- `floorDue` buys a replacement reserve after the floor is short for
  5 min. A resumed reserve still serving does not count as returning,
  so the original drains once it goes idle.

None of this, nor placement, looks at images:

- `scheduling.pack` is best-fit by free CPU and memory alone.
- `leavers` keeps the cheapest, then the smallest, idle hosts.
- `reserveFor` and `grow` choose by shape, market and price.
- Hosts do not report cached images: Hello names only platform images.

That is why torch start 3 (07:09:54) went to `c7f3`, a fresh m7i.large
that fits a 1-CPU container more tightly, instead of `7b69`, which had
read torch 2 minutes earlier.

What locality is worth, from the same traces (torch image 9b10…):

| Host | `agent.start` | lazy reads during the run |
| --- | --- | --- |
| first time on the host (06:56, 07:08, 07:09) | 1.6-4.8 s | 72-79 fetches, 300-330 MB missed, 4.8-5.3 s fetch wait |
| host that ran it before (07:11, 07:12) | 1.0 s | 0 fetches, 0 missed |

Proposed fixes:

- Image-affine placement. Among the hosts a container fits, prefer one
  that ran a container of the same image within the snapshotter's cache
  retention (a SQL projection over containers by host and image, rebuilt
  from Postgres; no new contract). Fall back to best-fit. Expected gain:
  about 5-6 s per torch cold start that now lands fresh (9.4 s toward
  Beam's 3.6 s), and about 0.3-0.5 s per python start (first-image mount
  and fill).
- Keep image-holding hosts. In `leavers`, keep the idle hosts that ran
  the recently used images before cheaper ones. In `reserveFor`, prefer
  a reserve that ran the image. Return idle hosts to the reserve, where a
  hibernated disk keeps its cache, before draining them. Expected gain:
  the same per start, for bursts spaced past the 5 min idle timeout.
- Count a resumed reserve that is serving as returning in `floorDue`, so
  a burst does not buy a replacement and then drain the original.
  Expected gain: fewer purchases (two in this window), no latency change.

## Gaps and unverified boundaries

- No scheduler log remains for the window. Which action removed `405e`
  at 07:03:40 (leave or another path) is inferred, not read.
- The runtime stage was measured on plain overlay images. The share of
  the lazy FUSE layers and of the link round trips (about 0.3-0.4 s in
  prod) is unmeasured.
- The locality gain comes from prod traces, not a placement change.
- The 8.1 s first pull on the us-east-1 reserve (one 7.3 s blob request)
  is a single sample and unexplained.
- Cleanup: the scratch hosts i-01ca94680c098eb4c and
  i-06215fc3f728d94a4 (about 1.1 h each, about $0.6 in all), the VPC
  vpc-0119f8672df3ef3b0 with its subnet, route table, internet gateway
  and security group, and the bucket parity-start-latency-534742592531
  are deleted. Nothing tagged `lazycloud:task=parity-start-latency`
  remains.

