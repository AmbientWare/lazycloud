"""Trivial deployable workload for the dispatch-latency benchmark.

This module is intentionally self-contained: the latency harness deploys it as
the source package for a real function so that container dispatch, startup, and
runner execution are exercised through the public path. Keeping it free of any
project imports lets the harness bundle only this single file, so the source
archive fetch stays minimal and the runner can import it directly.
"""

from __future__ import annotations


def noop(value: int = 0) -> int:
    """Return the input unchanged.

    The body does no meaningful work on purpose. The benchmark measures the
    cost of getting a container scheduled, started, and into the runner, not the
    cost of the user function itself, so the execution leg must be as close to
    zero as possible.
    """
    return value
