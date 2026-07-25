# Web Zod Contracts

These schemas are the manually synchronized browser half of `shared.http`.
Keep one domain file aligned with its Pydantic owner and export the inferred
type beside each schema. Model only consumed fields, exact list/nullability/time
semantics, and genuine tolerant defaults; never erase payloads with
`z.any()`/`z.unknown()` or broad records. Schemas stay pure—fetching, query keys,
and formatting live elsewhere. A contract change updates producer and consumer
together and is accepted by parsing a representative real response.
