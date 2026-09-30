# Worker repository

- Own the trusted server bridge from authenticated worker contracts to control-plane
  state and credential authorities. Never install/import this package in workers.
- Verify assignment, workspace, token, build and capability authority before
  mutation or credential vending. Worker claims are untrusted inputs.
- Keep worker payloads/client with the worker; no direct-persistence fallback.
