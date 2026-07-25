# Worker Bootstrap App

Own scheduler-worker settings and bootstrap sequencing; lifecycle behavior
belongs in `packages/worker` and `packages/scheduler`. Keep deployment
environment names aligned and preserve token secrecy, retry, and readiness when
accepting sequencing changes.
