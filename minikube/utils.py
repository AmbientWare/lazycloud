import shutil
import subprocess

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
    try:
        result = run_command(["minikube", "status"], check=False)
        return result.returncode == 0
    except subprocess.CalledProcessError:
        return False
