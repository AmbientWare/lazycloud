# Provider Client Composition

The mapping from persisted provider configuration and process settings to
concrete adapter instances.

This package owns construction and nothing else: not provider workflows, billing,
capacity reconciliation, API or CLI behavior, persistence tables, or process
entrypoints.

The production registry admits only providers with live evidence behind them. A
persisted kind that is not supported fails explicitly and by name; it never
resolves to a stand-in that appears to work. Keep exports explicit, and let
credential and construction failures surface rather than collapsing into a null
adapter that fails later somewhere less informative.
