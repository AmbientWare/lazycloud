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

Sources: X-Ray traces of the 06:40-07:25 UTC window (us-east-1, 21
`agent.start` traces with Docker and snapshotter spans), CloudTrail EC2
events in all four regions, and scratch hosts from
`ami-0104d7b9500423a78` in a scratch VPC (us-east-2a). The scheduler pods
of the window are gone (CloudTrail shows new pod sessions at 07:15 and
13:18) and no CloudWatch log group exists, so no scheduler log remains.

Interim (work in progress):

- Prod `agent.create` = Docker create + start (+ network policy). Warm
  host, cached image: 53-69 ms create, 210-290 ms start. First image on a
  host: start 0.5-0.8 s (lazy layer mounts and fills run inside it). The
  2.16 s c7i case (07:08:05, i-0a335ed829c4ede2a): create 611 ms, start
  1546 ms, on a Spot reserve resumed from hibernation 4.5 min earlier that
  had never run a container, with a 27-frame prefetch (2.2 s) in parallel.
- Scratch fresh hosts (no hibernation), plain `python:3.12-slim` under
  runsc: first container ever create 218/227 ms, start 532/433 ms (c7i/m7i);
  later ones 61-65 ms and 200-300 ms. First-container host cost is about
  0.4 s; untouched EBS files read at hydrated speed (no lazy-load penalty).
- Runner imports under runsc (m7i.large, bind-mounted runtime): `import
  runner.protocol` 737 ms first, 252 ms second in the same container
  (stdlib bytecode written by the first); runc 349/143 ms. A bare
  interpreter is 20 ms.

## Gaps and unverified boundaries
