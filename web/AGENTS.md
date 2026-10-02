# Dashboard

- Use the public Workspace → App → Workload → Task → Container model. Resolve
  relationships and classifications on the server; distinguish names from IDs.
- Keep one authenticated workspace shell and one owner per view. Account queries
  stay account-scoped; switching workspace does not switch session credentials.
- TanStack Query owns server state. Use typed, scoped keys and one authenticated,
  resumable stream; do not use unauthenticated EventSource or duplicate polling.
- Types come from contracts/openapi.yaml (`bun run apigen`); call the API through
  the typed `api` client and `ok()`. The server validates both directions against
  the same document, so do not restate its schemas in Zod. Use Zod only for values
  the contract leaves open and for typed settings. Fetch pricing and compute
  classifications from their server owners.
- Collections use cursor pagination and incremental scrolling, not unbounded
  fetches or client pagination. Scope subscriptions to the current selection.
- Use same-origin requests authenticated by the HttpOnly session cookie; the page
  never holds a credential. GitHub sign-in is a navigation to `/auth/github/start`,
  and the server's callback sets the cookie and redirects.
- Mask secrets, reveal only on explicit action and do not persist revealed values.
  Show newly issued credentials once. Never evaluate stored user payloads.
- Keep feature components local until genuinely reused; reuse focused shared
  controls and shadcn components instead of adding another design system.
- Use compact graphite styling, semantic status colors, sentence case and concrete
  labels. Avoid decorative badges, redundant cards and filler subtitles.
- Preserve keyboard navigation, Radix accessibility, reduced motion, predictable
  mobile layouts and panel scrolling. Show real progress, scoped loading states
  and useful errors rather than blanking the shell.
- Confirm destructive actions with the exact resource, permission or cost effect.
  Keep internal implementation detail out of user flows.
- Use Bun and its lockfile. Validate affected lint/types and rendered behavior
  when the UI boundary changes.
