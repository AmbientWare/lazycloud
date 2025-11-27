"""Test runner - excludes e2e tests by default."""

import subprocess
import sys


def main():
    """Run tests excluding e2e by default. Use pytest -m for full control."""
    args = sys.argv[1:]
    pytest_args = []

    # Handle coverage flag
    if "coverage" in args:
        args.remove("coverage")
        pytest_args.extend(
            [
                "--cov=lazycloud_api",
                "--cov-report=term-missing",
                "--cov-report=html:coverage_html",
            ]
        )

    # If user passed -m, don't override their marker selection
    if "-m" not in args:
        pytest_args.extend(["-m", "not e2e"])

    cmd = ["pytest"] + pytest_args + args
    sys.exit(subprocess.call(cmd))
