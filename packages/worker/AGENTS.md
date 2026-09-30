# Worker

- Own container execution, networking, mounts, metering and cleanup behind narrow
  protocols. Entrypoints stay in apps; control-plane access uses worker HTTP.
- Startup preparation during admission hold makes no control-plane calls.
  Complete local checks before readiness; registration failures still clean up.
  Background preparation must not keep a stopped process alive.
- Run every container under gVisor. Normalize persisted legacy runtime names to
  `runsc`; never add an unsandboxed execution path.
- GPU execution needs device nodes and matching driver libraries from the worker
  filesystem. Preserve sonames; mount individual libraries, not the worker's libc
  directory. Missing drivers fail explicitly.
- Keep gVisor, NVIDIA driver and GPU AMI pins compatible and validate the real
  sandbox when changing them.
- Give every acquired resource an owner and cleanup path, including partial
  startup, cancellation and failed credential/image operations.
- Writable layers use quota-enabled XFS with host free-space reserve. Disk limits
  are ceilings; admission checks both backing filesystem and sparse-image space.
- Retry metering windows with identical bounds and evidence. New samples belong
  to later windows; workers do not reserve money or restate billing authority.
- Put build children in their accounting cgroup before exec. Sample independently
  of delivery; final usage ends at process stop, not upload/result completion.
- Preserve accounting counters through guest exit; close the final sample before
  removing its parent cgroup. Missing counters mean incomplete evidence, not zero.
- Bill routed egress only with verified destination exclusions and owned interface
  evidence. Telemetry never changes firewall authorization; cleanup is scoped.
- Recover missed stop events through the authoritative cleanup list and normal
  finalization. End metering when recorded terminal evidence refuses later usage.
- Disk credentials follow the disk lease through final publication, independent
  of scheduler state. Renew lazy-read credentials until detach; failed reads stop
  the container with the appropriate unavailable/full reason.
- Nest containers under the worker's bounded cgroup and move worker processes to
  a leaf before enabling controllers. Read limits there, not from host CPU/RAM.
- Memory eviction uses worker pressure and excess over reservation; never choose
  a container within its request or evict from an unbounded host-wide signal.
- Resource-control changes require the production worker image, real runsc and
  cgroup filesystem evidence; unit checks cannot establish effective isolation.
