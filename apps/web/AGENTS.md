# Web Control Plane

`apps/web` is the browser control plane for the platform—an operational surface,
not a marketing site. Its product model is
`Workspace -> App -> Workload -> Task -> Container`, where function, endpoint,
ASGI, task queue, cron, pod, and sandbox are kinds of workload.

Customer surfaces speak in human names, kind, status, version, and time. IDs stay
available for URLs, queries, actions, and operator records. Relationships and
capabilities are resolved by the server, never assembled in the browser by
joining unrelated lists.

## Structure And Ownership

Product routes are workspace-scoped, and the workspace layout is what resolves
authentication and provides workspace context to everything beneath it. One
shell owns navigation, workspace switching, breadcrumbs, responsive layout, and
global search.

The signed-in person and the workspaces they reach come from one request, so the
two cannot disagree. Switching workspace never means switching credentials: a
session reaches every workspace its account belongs to.

Give each resource one owning surface. A detail view for a child does not
duplicate the logs, lifecycle, telemetry, or actions its parent already owns, and
an internal implementation step never becomes a customer-visible phase. Internal
platform concepts stay out of the customer model entirely.

Collections read through server cursors and continue automatically inside their
own scroll region. Do not fetch unbounded lists, slice results in the browser, or
add manual pagination controls. Keep per-resource stream state isolated by
selection so switching rows cannot mix two streams together.

Keep a component beside its feature until a second independent consumer proves
shared ownership. When it moves, move it to a focused shared folder rather than a
broad barrel, and delete the old re-export path.

## Data And Security

- Requests are same-origin, carry the auth-store bearer token, and are scoped to
  the active workspace. Sign-in is a username and password exchanged for a session
  credential; there is no paste-a-token path, because two ways in means two ways to
  keep working.
- Validate every JSON response against the hand-maintained Zod schemas that
  mirror the server contracts. A contract change updates both halves together.
- TanStack Query is authoritative for server records: option builders live in
  `lib/queries` with typed workspace keys, and one provider owns the resumable
  authenticated change stream. Do not copy server records into client stores or
  add inline resource fetches.
- Bearer-authenticated event streams go through the shared stream hook rather
  than native `EventSource`, which cannot carry the header.
- Payloads never carry environment dictionaries, injected credentials, or secret
  values. Secrets render masked; revealing one is explicit, row-local, kept out
  of persistent caches, and cleared on hide or delete. A newly created token
  secret is shown once.
- Preview stored user content by decoding safe text or describing the value.
  Never evaluate or deserialize it. Read environment variables only through the
  typed env modules.

## UI System

Quiet graphite surfaces, crisp sans typography, monospace for data, restrained
accent color for actions. Product routes fill the viewport below the shell, and
tables, logs, charts, terminals, and inspectors scroll inside their own region;
mobile falls back to one predictable content scroller.

- Prefer a few framed operational regions over nested decorative cards, badge
  mosaics, or dense grids of tiny metrics. A repeated resource becomes a card
  only when the whole card navigates.
- Use the shared status, icon, and radius primitives, and apply semantic color
  consistently rather than decoratively.
- Loading skeletons match the layout they replace; spinners are for inline
  waits. Route and panel error boundaries keep one failure from blanking the
  shell. Relative timestamps keep their exact machine-readable values.
- Respect native and Radix keyboard semantics and reduced-motion preferences.
  Accessibility, layout containment, and interaction outcomes are what matter;
  exact pixels, colors, and DOM adjacency are not.

## Tooling

Bun is the only package manager; keep the lockfile synchronized. Reuse the
existing shadcn primitives and add new generated ones through its CLI. Lint and
typecheck changed web code, and prove behavior with the cheapest rendered
evidence—Vitest for material pure boundaries, Playwright only for browser
behavior nothing cheaper can prove.
