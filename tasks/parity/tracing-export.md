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

- Prod exported all along. X-Ray us-east-1 holds 649 prod traces from
  2026-10-06 06:42Z (a minute after v0.1.96 deployed) to 14:07Z, 498 of
  them between 06:40Z and 08:00Z, with no 40-minute gap. Cold start
  1-f4a6b4cb-59cc15245ce7fb1ed0379647 has spans from the scheduler, server,
  agent and snapshotter, pulling from prod ECR on host 01a10ff5.
- Why a search found nothing: X-Ray names a segment after its service only
  for a server span, so the trace above is one segment named
  `execution.scale_up` and the agent's and snapshotter's spans are nameless
  subsegments. `service("lazycloud-scheduler")` and
  `service("lazycloud-agent")` return 0 over that window;
  `service("execution.scale_up")` returns 13.
- Ruled out by running the pinned collector and the chart's config locally
  against X-Ray with `AWS_PROFILE=default`: region, trace id format (the
  timestamp check is skipped by default), sampler, batch processor and the
  server's own spans all export. The Pod Identity association for
  `lazycloud-prod/lazycloud-otel-collector` and its `put-traces` policy
  exist. A credential failure logs `Exporting failed` at error level.
- Fix: a transform marks each span that starts a trace or continues one
  from another process as its process's local root, so each process gets
  its own segment named after it, with the span's name in the indexed
  `lazycloud.span` annotation. Verified on X-Ray: a three-process trace
  shows three segments, found by `service("lazycloud-repro-agent")` and
  `annotation[lazycloud.span] = "repro.fill"`.
- The collector serves its own metrics on port 8888 (`metrics`), admitted
  from `networkPolicy.metricsFrom`. Locally: 10 received, 10 sent with
  credentials; 5 received, 5 `otelcol_exporter_send_failed_spans` without.
- `deploy/check.sh` validates the rendered collector config with the
  pinned collector image.

## Gaps and unverified boundaries

- The prod trace after the Ship, with four process segments, is not
  recorded yet.
- X-Ray has no delete API. Test traces from services
  `lazycloud-tracesend`, `lazycloud-repro-*` and `lazycloud-server`
  (version `scratch`), sent 2026-10-06 14:18Z to 14:30Z, expire after 30
  days.
- Segments X-Ray drops at translation, or returns as unprocessed, are
  logged at debug level only and count as sent.
