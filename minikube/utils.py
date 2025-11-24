import shutil
import subprocess
import time
from typing import Callable

from rich.console import Console

from minikube.constants import REQUIRED_TOOLS

console = Console()


def run_command(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a command with error handling."""
    try:
        return subprocess.run(cmd, check=check, capture_output=True, text=True)

    except subprocess.CalledProcessError as e:
        if check:  # Only show error if we're supposed to check
            console.print(f"[red]Command failed: {' '.join(cmd)}[/red]")
            if e.stderr:
                console.print(f"[red]Error: {e.stderr.strip()}[/red]")
        raise


def check_prerequisites() -> bool:
    """Check if all required tools are installed."""
    missing = [tool for tool in REQUIRED_TOOLS if not shutil.which(tool)]

    if missing:
        console.print("[red]Missing required tools:[/red]")
        for tool in missing:
            console.print(f"  [yellow]•[/yellow] {tool}: {REQUIRED_TOOLS[tool]}")
        return False
    return True


def is_minikube_running() -> bool:
    """Check if minikube is currently running."""
    # Check if container is running (more reliable than minikube status when on multiple networks)
    try:
        result = run_command(
            ["docker", "ps", "--filter", "name=minikube", "--format", "{{.Names}}"],
            check=False,
        )
        if result.returncode != 0 or "minikube" not in result.stdout:
            return False

        # Verify kubectl can connect (actual test of cluster health)
        kubectl_result = run_command(["kubectl", "get", "nodes"], check=False)
        return kubectl_result.returncode == 0
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def retry_until(
    condition: Callable[[], bool],
    max_retries: int = 10,
    delay: float = 2.0,
    description: str = "operation",
) -> bool:
    """Retry a condition until it's true or max retries reached."""
    for attempt in range(max_retries):
        if condition():
            return True
        if attempt < max_retries - 1:
            console.print(
                f"[yellow]Waiting for {description}... (attempt {attempt + 1}/{max_retries})[/yellow]"
            )
            time.sleep(delay)
    return False
