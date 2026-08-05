# Benchmarks

Measurements of current production entrypoints, run from outside the services
they measure.

A benchmark never changes the contract it measures and never relaxes a
correctness check to go faster—a number produced by a path that no longer does
the work is worse than no number at all.

Make inputs, concurrency, timeouts, and output format explicit rather than
inherited from defaults, so a result can be compared to another run. Never embed
secrets. Mark runs that depend on an external provider as external, and report a
skipped expensive run as skipped rather than omitting it silently.
