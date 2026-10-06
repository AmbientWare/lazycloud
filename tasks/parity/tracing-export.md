# Tracing export

## Scope

Prod recorded zero traces in X-Ray on v0.1.96 while the collector ran with
no logged errors. Find why (the awsxray exporter's region, its Pod
Identity credentials, the receiver getting nothing, sampling) and fix it, so
a cold start's trace from server, scheduler, agent and snapshotter appears
in X-Ray. Owns the collector config, its helm templates and the collector's
Terraform.

## Evidence to record

- A prod cold start's trace in X-Ray with spans from all four processes
  (after the Ship).
- The collector's own export metrics showing sent and failed spans.

## Progress

## Gaps and unverified boundaries
