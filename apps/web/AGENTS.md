# Web Control Plane

`apps/web` is the browser control plane, not a marketing site. Its product model
is `Workspace -> App -> Workload -> Task -> Container`; function, endpoint,
ASGI, task queue, cron, pod, and sandbox are workload kinds. Customer surfaces
use human names, kind, status, version, and time; IDs remain available for URLs,
queries, actions, and operator-only records. Relationships and capabilities come
from the server, never browser joins over unrelated workspace lists.

## Navigation And Ownership

- Product routes are workspace-scoped under `/w/$workspace`; the workspace
  layout resolves auth and provides `useWorkspace()`. Root lands on Apps.
- `AppShell` owns navigation, workspace switching, breadcrumbs, responsive
  layout, and global search. Primary destinations are Apps, Tasks, Storage, and
  Usage; Settings owns workspace Compute. Fleet administration remains API/CLI
  only unless explicitly reopened.
- Apps use the single summaries read. App detail is a continuous operational
  page: workloads, activity/recent Tasks, then app-scoped sandboxes. Workload
  detail remains kind-specific; do not duplicate Task-owned logs, lifecycle,
  telemetry, or actions.
- `/tasks` is the server-filtered, cursor-paginated workspace feed. `TaskDrawer`
  is shared across global/app/workload routes and owns authorized actions,
  timing/lifecycle, logs, output, trace, and container inspection. Lifecycle
  shows Queued, Container preparation, and Execution; internal preparation
  events do not become extra customer phases.
- Execution containers are inspected through their Task, not a standalone
  customer route or search result. Sandbox detail owns Terminal, Files,
  Processes, and Network. Shell uses the authenticated WebSocket proxy and
  never exposes helper credentials.
- Storage owns live volumes, secrets, queues, and maps. Usage owns one exact
  server-resolved date window and server-defined billing/attribution. Settings
  owns workspace identity/deletion, scoped expiring tokens, and customer compute
  configuration; internal cloud pools never become customer workload concepts.

Collections use server cursors and automatic continuation inside their owning
scroll region; do not fetch unbounded lists, browser-slice results, or add manual
pagination controls. Keep task/log stream state isolated by selected Task.

## Security And Data

- Use same-origin FastAPI requests with the auth-store bearer token and
  `withWorkspace`. Validate every JSON response with hand-maintained Zod schemas
  synchronized with `shared.http`.
- TanStack Query is authoritative for server records. Put option builders in
  `lib/queries`, use typed workspace keys, and let
  `WorkspaceLiveUpdatesProvider` own one resumable authenticated change stream.
  Do not copy server records into Zustand or create inline resource fetches.
- Keep dedicated transports for SSE logs, shell, focused metrics, hot
  collections, storage paths, and sandbox operations. Bearer-authenticated SSE
  uses `useEventStream`, not native `EventSource`.
- Deployment and container payloads never expose environment dictionaries,
  injected credentials, or secret values. Secret lists stay masked; reveal is
  explicit, workspace-scoped, row-local, absent from persistent query caches,
  and cleared on hide/delete. Token secrets appear once after creation.
- Queue/map previews decode safe JSON/text or describe binary values; never
  evaluate or unpickle content. Read environment variables only through typed
  T3 Env modules.

## UI System

Use quiet graphite surfaces, crisp Instrument Sans typography, JetBrains Mono
for data, and restrained indigo actions. Product routes fill the viewport below
shell chrome; tables, logs, charts, terminals, and inspectors scroll inside
their owner. Mobile uses one predictable content scroller.

- Use a few framed operational regions, not nested decorative cards, badge
  mosaics, radial color bursts, or tiny KPI grids. A repeated resource may be a
  card only when the whole card navigates.
- `StatusChip` is the filled status treatment. Use semantic color consistently,
  lucide action icons with accessible labels, and the shared small-radius token.
- Loading skeletons match layout; spinners are for inline waits. Shared route
  and panel error boundaries prevent shell white screens. Relative timestamps
  retain exact semantic `title`/`dateTime` values.
- Respect native/Radix keyboard semantics and reduced motion. Accessibility,
  layout containment, and interaction outcomes matter; literal pixels, colors,
  DOM adjacency, and decorative snapshots do not.

Keep components beside their feature until independent consumers prove shared
ownership. Shared app components use focused folders without a broad barrel;
delete old re-export paths when ownership moves. `TaskTable`, `TaskDrawer`,
`ContainerMetricsCharts`, `LinearSelect`, and `InfiniteScrollBoundary` retain
their established cross-feature ownership.

## Tooling And Acceptance

Bun is the only package manager; keep `bun.lock` synchronized. Reuse existing
shadcn primitives and add generated primitives through its CLI. For changed web
code, lint/typecheck and select the cheapest unique rendered evidence. Use
Vitest for material pure boundaries and Playwright only for browser behavior
that cheaper evidence cannot prove. Contract changes require one authenticated
request and the affected rendered outcome; rebuild images only when their
contents or serving composition changed.
