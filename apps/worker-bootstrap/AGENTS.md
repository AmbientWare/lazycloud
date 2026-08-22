# Worker bootstrap app

Settings and bootstrap sequencing for scheduler-managed workers.

Lifecycle behavior belongs in `packages/worker` and `packages/scheduler`. What
this app owns is the order things happen in and what must hold before the
process reports ready: tokens stay secret, retries stay bounded and visible, and
readiness means the worker can actually take work rather than that it started.
