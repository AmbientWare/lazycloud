# SDK abstractions

The decorator-facing resource objects a user holds, built over narrow client
protocols.

Each abstraction translates a transport error into its own typed operation error
exactly once, preserving the cause. Translating twice loses the original;
translating nowhere leaks transport detail into user code.

Invocation targeting goes through the shared resolver, and an already-bound
identifier wins over name resolution. A handle the user already has must not
silently retarget.

Depend only on `shared`, the SDK clients and session, and sibling abstractions.
Report progress through terminal hooks, never by printing directly: a library
that writes to stdout is a library nothing can embed.
