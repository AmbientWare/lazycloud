# Public Ingress

The `cloudflared` sidecar that puts the control plane on the public internet.
`cloudflared.yml` is the entire public surface: the tunnel is locally managed, so
nothing outside this file adds or removes a route. `README.md` covers operating
and rotating it.

- A change to this file is a change to what the internet can reach. Review it as
  one, and keep it small enough to review that way.
- The ingress list is ordered and first match wins, so a rule placed after the
  catch-all is dead.
- Refusing something at the edge removes a surface; it never replaces the
  origin's own authorization. Do not relax an origin check because a prefix is
  blocked here.
- The wildcard host forwards every path. Generated per-application hosts are
  rewritten to their handler path upstream, so a path filter here breaks user
  applications rather than restricting them.
- A wildcard certificate covers exactly one label, which is why the base host
  stays at the zone apex.
- The tunnel identifier belongs in the repository; the tunnel secret lives only
  in the credentials file the deployment points at, outside the repository.
