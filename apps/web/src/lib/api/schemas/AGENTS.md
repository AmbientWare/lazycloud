# Web Zod contracts

The browser half of the shared HTTP contracts, maintained by hand.

Keep one domain file aligned with its Pydantic owner and export the inferred type
beside each schema. Model the fields the browser consumes with their exact list,
nullability, and time semantics, and give a default only where the wire contract
genuinely has one. Preserve the distinction between an omitted value and a zero.

Never erase a payload with `z.any()`, `z.unknown()`, or a broad record. A field
that parses everything validates nothing, and the mismatch it hides shows up as a
rendering bug far from its cause.

Schemas stay pure: fetching, query keys, and formatting live with their own
owners. A contract change updates producer and consumer in the same change.
