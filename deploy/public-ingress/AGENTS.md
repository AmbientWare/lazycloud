# Public Ingress

The `cloudflared` service that puts the control plane on the public internet.
`cloudflared.yml` is the entire public surface: the tunnel is locally managed, so
nothing outside this file adds or removes a route. `README.md` covers operating
and rotating it.

- A change to this file is a change to what the internet can reach. Review it as
  one, and keep it small enough to review that way.
- The ingress list is ordered and first match wins, so a rule placed after the
  final catch-all is dead.
- The final rule forwards rather than refusing, because customer-owned hostnames
  arrive carrying their own `Host` header and cannot be enumerated here. What the
  platform serves is still decided at the origin, which routes a known hostname to
  its resource and authorizes everything else for itself. Narrowing this rule
  breaks custom domains.
- The origin cannot refuse by hostname on the edge's behalf. Internal callers
  reach the control plane by service name, so "not the public domain" describes
  the worker registering itself exactly as well as it describes a stranger.
  Anything that must not be public is authorized, not hidden.
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
