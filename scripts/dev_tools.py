"""Development tools and utilities."""

import subprocess
import sys


def textual_console():
    """Launch Textual console with DEBUG logging."""
    try:
        subprocess.run(
            [
                "textual",
                "console",
                "-x",
                "DEBUG",
                "-x",
                "EVENT",
                "-x",
                "WORKER",
                "-x",
                "SYSTEM",
                "-x",
                "INFO",
            ],
            check=True,
        )
    except KeyboardInterrupt:
        sys.exit(0)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)


def cli_dashboard():
    """Launch the LazyCloud CLI dashboard."""
    try:
        subprocess.run(
            [
                "textual",
                "run",
                "--dev",
                "apps/cli/src/cli/ui/textual/dashboard/main.py",
            ],
            check=True,
        )

    except KeyboardInterrupt:
        sys.exit(0)

    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
