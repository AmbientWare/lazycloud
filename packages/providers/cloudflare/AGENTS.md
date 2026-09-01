# Cloudflare provider

Custom hostnames for one Cloudflare zone: the edge that terminates TLS for domains
a customer owns rather than ones this platform issued.

- Act on the hostname identifier the provider assigned, not on the hostname itself.
  The name belongs to the customer and can be registered again by someone else; the
  identifier names the object this platform created.
- A registration names the exact hostname the provider terminates. Wildcard customer
  registrations are not part of the public contract.
- Deleting a hostname the provider no longer holds is success, not an error: the
  caller asked for absence and absence is what it got.
- A rejected hostname is the caller's input and a failed edge is not, so they raise
  different errors. Only one of them is worth retrying.
- The API token grants everything this adapter can do. It never enters a log, an
  error message, or a repr.
