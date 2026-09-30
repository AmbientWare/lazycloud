# Agent

- Own installation, daemon lifecycle, enrollment and telemetry. Process startup
  stays in `apps/agent`; backend implementations stay behind narrow protocols.
- Worker control owns Docker slots and reserve records; local state owns identity
  and readiness. Reuse one worker observation per reconciliation stream.
- Route notifications apply changes; snapshots recover revision gaps/reconnects.
  Avoid route scans on idle heartbeats. Probe independently of worker control and
  name the checked target in readiness.
