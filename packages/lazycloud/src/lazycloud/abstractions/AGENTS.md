# SDK abstractions

- Build decorator-facing objects on narrow client protocols; depend only on shared
  contracts, SDK clients/session and sibling abstractions.
- Translate transport errors once, preserving causes. Use the shared invocation
  resolver; an already-bound identifier must not silently retarget.
- Report progress through terminal hooks, never direct library stdout writes.
