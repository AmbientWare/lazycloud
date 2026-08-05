# Execution Package

User execution resources: task, artifact, volume, and secret workflows, the
deterministic planners behind them, and the collections and signals they
coordinate through.

Planners stay pure—same inputs, same plan, no I/O—so what they decide can be
reasoned about without running it. Services take explicit protocols and explicit
arguments and raise typed domain errors.

Concrete scheduler, worker, gateway, provider, app, and SDK implementations stay
outside. Persistence, authorization, retries, and cleanup are invariants of this
package: a task that fails still has to leave the workspace consistent and its
resources released.
