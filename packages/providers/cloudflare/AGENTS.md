# Cloudflare provider

Custom hostnames for one Cloudflare zone: the edge that terminates TLS for domains
a customer owns rather than ones this platform issued.

- Act on the hostname identifier the provider assigned, not on the hostname itself.
  The name belongs to the customer and can be registered again by someone else; the
  identifier names the object this platform created.
- A wildcard registration covers exactly one label, which is what its certificate
  secures. Widening that here would promise more than TLS delivers.
- Deleting a hostname the provider no longer holds is success, not an error: the
  caller asked for absence and absence is what it got.
- A rejected hostname is the caller's input and a failed edge is not, so they raise
  different errors. Only one of them is worth retrying.
- The API token grants everything this adapter can do. It never enters a log, an
  error message, or a repr.
