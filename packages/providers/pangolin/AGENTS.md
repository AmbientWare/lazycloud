# Pangolin provider

Pangolin owns private site identities and the public edge. This adapter uses the
documented Integration API only. It never calls dashboard session routes or reads
Pangolin's database.

- Store Pangolin identifiers, not names, for later reads and deletion.
- Treat deleting an absent site, domain, resource, or target as success.
- Keep site secrets in `SecretStr` values and return them only from the one
  enrollment call that needs to deliver them to Newt.
- Domain registration preserves the customer's explicit CNAME or NS delegation
  choice. Return Pangolin's DNS records without reconstructing them in LazyCloud.
- A failed cleanup remains durable work. Do not report local deletion while the
  Pangolin object still exists.
