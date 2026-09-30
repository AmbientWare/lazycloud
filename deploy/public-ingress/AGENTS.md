# Public ingress

- Keep cloudflared routes ordered for first match, including all wildcard paths
  and the final forwarding route for custom hostnames.
- Origins enforce authentication independently; a hostname is not authorization.
- Wildcard certificates cover one label, not the apex. Keep credentials outside
  the repository and reference their file explicitly.
