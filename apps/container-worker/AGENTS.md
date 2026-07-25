# Container Worker App

Own worker settings, dependency assembly, process loop, and container-service
HTTP adapter. Execution behavior belongs in `packages/worker`.

Production workers reach control-plane state and credential vending only
through the authenticated worker-repository API. They retain worker-owned OCI,
cache, mounted-storage, filesystem, and presigned-transfer data paths; never add
direct database, Redis, scheduler, identity, control, observability, or image
service access. Validate repository URL/token before registration and never
fall back to persistence after connection loss. Runner/shared contract changes
require rebuilding the versioned worker image before containerized acceptance.
