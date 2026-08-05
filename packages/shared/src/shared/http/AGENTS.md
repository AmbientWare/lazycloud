# Shared HTTP Contracts

Every public JSON payload consumed by the API, the SDK and CLI, the runner, or
the web dashboard.

Keep one domain module and one precise `HttpModel` per wire payload. Use
`datetime` rather than strings, closed enums rather than open ones, `{data, next}`
for lists, explicit encoding for bytes, and model validators for invariants the
type system cannot state on its own.

Failures use HTTP status codes and the shared error response. A status carried
inside a stream event is domain data and stays that way.

A contract has producers and consumers on both sides of the repository, so a
change updates the API routes and services, the SDK and CLI, the runner, the
exports, and the web schemas together. Delete models nothing consumes.
