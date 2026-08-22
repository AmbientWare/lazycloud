# Tests

Whether a test should exist at all is decided by the test decision gate in the
root `AGENTS.md`. This file covers where tests live and what they may assert.

- Owner tests live beside their package or app. `tests/integration` owns concrete
  cross-owner workflows, and architecture or deployment tests exercise real
  boundaries rather than scanning the repository. Only shared fixtures live at
  the root.
- The root autouse fixture stays limited to cheap environment and settings
  isolation. Pull in the full service graph only through the owner or integration
  `conftest.py` that actually needs it.
- Make production behavior work first. Assert the resulting state, response,
  error, side effect, resource residue, or cleanup. Do not assert construction,
  constants, types, signatures, calls, ordering, plans, commands, mocks, or
  source inventories.
- Prove each invariant once, at its cheapest authoritative owner. Duplicate a
  scenario higher up only for a boundary risk the lower test cannot see.
- Reserve broad matrices for distinct authorization, irreversible data-loss,
  paid-resource leak, concurrency, fencing, or destructive-cleanup transitions.
- Fakes stay typed and keep real error semantics: clients raise `HttpApiError`,
  services raise `shared.errors`. A fake that fails more politely than the real
  thing hides the handling that actually matters.
- Tests stay deterministic and require external credentials only when explicitly
  marked integration or smoke.
- Delete compatibility, historical-migration, wrapper-only, presentation-literal,
  snapshot, repository-policy, inventory, and implementation-shape tests when you
  encounter them. Accessibility and layout tests stay only for a material user
  outcome nothing cheaper proves.
