# Image parity with beam.cloud, second pass

These files live on `parity-plan`, the integration branch. They never merge
to main: the final PR removes `tasks/parity` first. The method is
PLANNING.md.

## Goal

Image-pull and build performance at parity with Beam, nothing more. The
2026-10-06 prod benchmark (v0.1.96, same images, cpu=1, us-east-2 hosts):

| | LazyCloud | Beam |
| --- | --- | --- |
| python deploy / cold / warm | 1.4 s / 1.78 s / 84 ms | 2.2 s / 1.60 s / 97 ms |
| torch deploy (build) | 69.7 s | 22.2 s |
| torch cold start, median of 3 | 9.4 s | 3.6 s |

Acceptance reruns that benchmark; only measured gaps get more work.

## Decisions

The user decided on 2026-10-06:

- Fix every gap the benchmark and the day's checks found.
- Builds install packages with uv, never pip.
- Base Python comes from uv's managed builds (python-build-standalone) on a
  pinned `debian:trixie-slim`, as before the rewrite, not Docker Hub's
  `python:<version>-slim`.

Integrator defaults, taken unless the user objects:

- The platform's Python base images are built by Ship, pushed to the
  platform registry and converted by the server at start like every
  platform image. Custom images without their own base build on them, so
  their base layers are shared and already converted.
- The standard library is compiled to bytecode in the base, as the old app
  did.

## Packets

| Packet | Branch | Migration | Shared-file range | Depends on |
| --- | --- | --- | --- | --- |
| [base-python](base-python.md) | `parity-base-python` | none | `internal/images/render.go`, managed template config, Ship workflow | none |
| [build-speed](build-speed.md) | `parity-build-speed` | none | the agent's build path, `internal/imagefs` upload | none |
| [prefetch-window](prefetch-window.md) | `parity-prefetch-window` | none | snapshotter trace, agent start path | none |
| [start-latency](start-latency.md) | `parity-start-latency` | none | scratch first, then the owning packages | none |
| [tracing-export](tracing-export.md) | `parity-tracing-export` | none | collector config, helm, Terraform | none |
| [lifecycle-stage](lifecycle-stage.md) | `parity-lifecycle-stage` | one file if needed | contract, execution, web task page | none |
| [test-store](test-store.md) | `parity-test-store` | none | test configs, compose.test.yaml | none |
| [acceptance](acceptance.md) | none | none | none | all |

## Waves

1. Wave 1, in parallel: base-python, build-speed, prefetch-window,
   start-latency (measure first), tracing-export, lifecycle-stage,
   test-store. Owned files do not overlap; base-python and build-speed both
   touch the build path, so build-speed stays in the agent's build runner
   and upload while base-python owns the Dockerfile renderer.
2. Wave 2: anything start-latency's measurements call for.
3. Final: one integration review of `parity-plan` against main, one
   migration file, remove `tasks/parity`, one PR, one Ship with a fleet
   replacement, then acceptance in prod.

## Agent protocol and merge gate

As in PLANNING.md. Every brief says whether sub-agents are allowed
(default no). Tests that touch an object store use their own store, never
the user's `lazycloud-local` stack. Real EC2 runs in `AWS_PROFILE=default`,
US regions only, tagged `lazycloud:task=<packet>`, removed before the
report. Each packet passes `./check.sh`, focused race tests, an
independent review with a focus list, and green CI before it merges into
`parity-plan`.
