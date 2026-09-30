# Container worker process

- Compose `packages/worker`; authenticate through worker-repository contracts.
  Never give the worker direct control-plane database or Redis credentials.
- Preserve data, image, mount and direct-transfer paths.
- Validate registration and surface typed failures; no local persistence fallback.
