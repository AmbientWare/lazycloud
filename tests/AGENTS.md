# Tests

Whether a test exists at all is decided by the root test decision gate. This
file adds where tests live and what they may assert.

- Owner tests live beside their package/app. `tests/integration` owns concrete
  cross-owner workflows; architecture/deployment tests exercise real boundaries,
  not repository scans. Shared fixtures alone live at the root.
- The root autouse fixture stays limited to cheap environment/settings
  isolation. Import `isolated_services` through an owner/integration
  `conftest.py` only where the full API graph is required.
- Make production behavior work first. Assert resulting state, response, error,
  side effect, resource residue, or cleanup—not construction, constants, types,
  signatures, calls, ordering, plans, commands, mocks, or source inventories.
- Prove each invariant once at its cheapest authoritative owner. Duplicate a
  scenario at integration/E2E only for a distinct boundary risk.
- Reserve broad matrices for distinct authorization, irreversible data-loss,
  paid-resource leak, concurrency, fencing, or destructive-cleanup transitions.
- Fakes remain typed and use real error semantics: clients raise `HttpApiError`;
  services raise `shared.errors`. Tests remain deterministic and require
  external credentials only when explicitly marked integration/smoke.
- Remove compatibility, historical migration, wrapper-only, presentation
  literal, snapshot, repository-policy, route/import/export inventory, and
  implementation-shape tests. Accessibility and layout tests remain only for a
  material user outcome not proven more cheaply.

Run only the focused owner or integration selection chosen as acceptance; do
not run suites as a procedural completion step.
