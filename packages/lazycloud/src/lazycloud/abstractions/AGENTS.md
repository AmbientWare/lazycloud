# SDK Abstractions

Own decorator-facing resource objects over narrow client protocols. Each
abstraction translates `RuntimeError` (including `HttpApiError`) once into its
typed operation error with the cause preserved. Invocation targeting uses
`resolve_invocation_target`; an already bound `stub_id` wins over name
resolution. Depend only on shared, SDK clients/session, and sibling
abstractions. Progress uses terminal hooks, never direct printing. Exercise
targeting/serve/deployment-binding changes through a representative public
deploy plus remote operation.
