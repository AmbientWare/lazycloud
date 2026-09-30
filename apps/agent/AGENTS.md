# Agent process

- Compose `packages/agent`; keep worker control on the main thread.
- Shutdown stops new starts independently of a blocked stream. An in-flight
  start cleans up its own container; only worker-slot files need the slot lock.
- Bound Docker startup waits and wake the controller between streams.
- Keep installer environment names aligned with process settings.
