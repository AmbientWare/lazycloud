# Operations package

Management workflows genuinely reused by more than one caller: API, CLI, or
gateway.

A workflow earns a place here by having a real second consumer, not by looking
reusable. Depend on typed service protocols and explicit repositories, not on
apps, SDK, runner, providers, or a broad service graph.

Raise typed domain errors, and preserve authority checks, retry behavior,
idempotency, and cleanup. These are the workflows an operator reaches for when
something has already gone wrong, so partial completion has to be safe to repeat.
