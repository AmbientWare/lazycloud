# Worker Repository Package

Own trusted server-side bridges from authenticated worker HTTP contracts to
control-plane state, credential authorities, checkpoints, and image builds. It
must never be installed in or imported by container workers. Worker payloads and
client stay in `worker`; database/Redis/identity/control/image adapters stay
here. Enforce assignment, workspace, token, build, and capability authority
before vending credentials or mutating state; never add worker persistence
fallbacks. Accept through an authenticated worker request and preserve denial,
credential, durability, and cleanup boundaries.
