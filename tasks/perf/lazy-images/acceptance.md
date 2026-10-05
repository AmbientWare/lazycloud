# Acceptance

## Scope

Prove lazy images on real hosts and record the numbers the peer cache
decision needs. Owns no product code; fixes go back to the owning packet.

## Plan

- Fresh EC2 host from the new node image, `AWS_PROFILE=default`: first
  container start for a plain Python image and a torch image, before
  (current main) and after (`perf-plan`), three runs each.
- Warm-host cold starts, unchanged or better.
- Everything the full pull did still holds: private registry images, GPU
  images, builds that use a lazily mounted base, pods, dev boxes, adoption
  after an agent restart.
- After the Ship, the same numbers in prod with `/tmp/lcb2`.

## Evidence to record

## Gaps and unverified boundaries
