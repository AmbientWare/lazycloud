from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Generator
from time import perf_counter

import pytest
from _pytest.terminal import TerminalReporter


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(SuiteTimings())


class SuiteTimings:
    def __init__(self) -> None:
        self.phases: dict[str, float] = defaultdict(float)
        self.fixtures: Counter[str] = Counter()
        self.fixture_seconds: dict[str, float] = defaultdict(float)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.phases[report.when] += report.duration

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup[ValueT](
        self, fixturedef: pytest.FixtureDef[ValueT]
    ) -> Generator[None, ValueT, ValueT]:
        started = perf_counter()
        try:
            return (yield)
        finally:
            self.fixtures[fixturedef.argname] += 1
            self.fixture_seconds[fixturedef.argname] += perf_counter() - started

    def pytest_terminal_summary(self, terminalreporter: TerminalReporter) -> None:
        terminalreporter.write_sep("=", "suite lifecycle")
        terminalreporter.write_line(
            " / ".join(
                f"{phase}: {self.phases[phase]:.2f}s" for phase in ("setup", "call", "teardown")
            )
        )
        slowest = {
            name for name, _ in sorted(self.fixture_seconds.items(), key=lambda row: -row[1])[:8]
        }
        resources = {"api_runtime", "service_context", "committed_service_context"}
        for name in sorted(slowest | (resources & self.fixtures.keys())):
            seconds = self.fixture_seconds[name]
            terminalreporter.write_line(f"{name}: {self.fixtures[name]} setups, {seconds:.2f}s")
