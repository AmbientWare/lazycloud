# Benchmarks

Measure current production entrypoints without changing their contracts. Keep
harnesses outside production services, inputs/concurrency/timeouts/output
explicit, and correctness checks intact. Mark provider-dependent runs external,
never embed secrets, and report skipped expensive runs.
