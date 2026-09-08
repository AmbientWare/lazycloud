# Cloudflare provider

R2 owns application object storage and temporary credentials scoped to one
workspace bucket. One deployment settings object supplies its account and parent
credential pair. Parent credentials stay inside the control plane; workers receive
only expiring workspace grants. Custom hostnames use a separate zone API token
because their permissions differ from R2 storage administration.

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
