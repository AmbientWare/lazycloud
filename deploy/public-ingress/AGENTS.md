# Public ingress

`cloudflared.yml` is the whole public surface. The tunnel is locally managed
(`config_src: local`), so the dashboard pushes nothing and adding a public
hostname there changes no routing while appearing to. Any change to what the
internet can reach is a change to this file and is reviewed here.

- The ingress list is ordered and first-match-wins. A rule appended after the
  catch-all is dead. `path` is an anchored Go regex.
- An edge refusal removes a surface; it never replaces the origin's
  authorization. Do not relax an origin check because a prefix is blocked here.
- `*.lazycloud.dev` must forward every path. Generated invoke hosts are rewritten
  to the stub's handler path by the control plane's host-routing middleware, so a
  path filter breaks user applications rather than restricting them.
- The base host must stay the apex. Universal SSL covers exactly one wildcard
  level, so a base of `api.lazycloud.dev` puts invoke hosts at
  `*.api.lazycloud.dev`, outside the certificate.
- The tunnel id in this file is an identifier and belongs in the repository. The
  tunnel secret lives only in the credentials file named by
  `LAZYCLOUD_PUBLIC_INGRESS_CREDENTIALS_FILE`, outside the repository.
- The container runs as uid 65532. A credentials file at mode `0400` owned by
  the operator cannot be read inside it; the parent directory holds the
  restriction instead. See `README.md`.
- This sidecar shares the control plane's network namespace, so recreating
  `control-plane` destroys it and Compose does not bring it back. See
  `deploy/AGENTS.md`.
