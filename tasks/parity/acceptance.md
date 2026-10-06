# Acceptance

## Scope

After the Ship and fleet replacement, rerun the 2026-10-06 parity benchmark
in prod with the same images on both platforms: python slim and torch CPU,
cpu=1, deploy (build), first call after deploy, cold start after
scale-to-zero (three runs, alternating order), warm p50, with each
LazyCloud cold start's lifecycle stages and its X-Ray trace. Smoke-test a
function with no image, an endpoint, a custom image and a pod.

## Evidence to record

## Gaps and unverified boundaries
