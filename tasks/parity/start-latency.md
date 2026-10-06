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

## Gaps and unverified boundaries
