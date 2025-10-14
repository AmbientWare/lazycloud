#!/usr/bin/env python3

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

console = Console()
app = typer.Typer(help="Manage LazyCloud Minikube development environment")

# Constants
REQUIRED_TOOLS = {
    "minikube": "https://minikube.sigs.k8s.io/docs/start/",
    "kubectl": "https://kubernetes.io/docs/tasks/tools/",
    "helm": "https://helm.sh/docs/intro/install/",
}

MINIKUBE_ADDONS = [
    "ingress",
    "storage-provisioner",
    "default-storageclass",
    "metrics-server",
    "gvisor",
]

STORAGE_CLASS_YAML = """
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: efs-sc
  annotations:
    storageclass.kubernetes.io/is-default-class: "false"
provisioner: k8s.io/minikube-hostpath
parameters:
  type: Directory
volumeBindingMode: Immediate
allowVolumeExpansion: true
reclaimPolicy: Retain
""".strip()


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


def cleanup_existing_cluster() -> None:
    """Clean up existing minikube cluster."""
    console.print("🗑️ [bold yellow]Deleting existing cluster...[/bold yellow]")
    try:
        if is_minikube_running():
            console.print("🛑 [bold]Stopping existing cluster...[/bold]")
            run_command(["minikube", "stop"], check=False)

        run_command(["minikube", "delete"], check=False)
        console.print("[green]✓ Existing cluster deleted[/green]")
    except subprocess.CalledProcessError:
        console.print("[yellow]⚠ Cluster deletion had issues (continuing)[/yellow]")


def setup_storage_class() -> None:
    """Create EFS-compatible StorageClass."""
    console.print("🔧 [bold]Creating EFS-compatible StorageClass...[/bold]")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(STORAGE_CLASS_YAML)
        temp_file = f.name

    try:
        run_command(["kubectl", "apply", "-f", temp_file])
        console.print("[green]✓ EFS-compatible StorageClass 'efs-sc' created[/green]")
    except subprocess.CalledProcessError:
        console.print("[yellow]⚠ Failed to create storage class (continuing)[/yellow]")
    finally:
        os.unlink(temp_file)


def setup_test_namespace() -> None:
    """Create test namespace."""
    console.print("🔧 [bold]Creating test namespace...[/bold]")

    result = run_command(
        ["kubectl", "create", "namespace", "lazycloud-test"], check=False
    )
    if result.returncode == 0:
        console.print("[green]✓ Test namespace 'lazycloud-test' created[/green]")
    elif "already exists" in result.stderr:
        console.print(
            "[yellow]✓ Test namespace 'lazycloud-test' already exists[/yellow]"
        )
    else:
        console.print("[yellow]⚠ Test namespace creation failed (continuing)[/yellow]")


def verify_gvisor_runtime() -> None:
    """Verify gVisor RuntimeClass is available."""
    console.print("🔧 [bold]Verifying gVisor runtime...[/bold]")

    try:
        # Check if RuntimeClass exists (created by addon)
        result = run_command(["kubectl", "get", "runtimeclass", "gvisor"], check=False)
        if result.returncode == 0:
            console.print("[green]✓ gVisor RuntimeClass is available[/green]")
        else:
            console.print("[red]❌ gVisor RuntimeClass not found![/red]")
            console.print("[yellow]  Try: minikube addons enable gvisor[/yellow]")
    except subprocess.CalledProcessError:
        console.print("[yellow]⚠ Could not verify gVisor RuntimeClass[/yellow]")


def configure_localstack_registry_dns() -> None:
    """Configure minikube to resolve LocalStack registry hostname."""
    console.print("🔧 [bold]Configuring LocalStack registry DNS...[/bold]")

    try:
        # Get LocalStack container IP on the lazycloud network
        result = run_command(
            [
                "docker",
                "inspect",
                "lazycloud-localstack",
                "--format={{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            ],
            check=True,
        )
        localstack_ip = result.stdout.strip()

        if not localstack_ip:
            console.print("[yellow]⚠ Could not find LocalStack container IP[/yellow]")
            return

        console.print(f"  LocalStack IP: {localstack_ip}")

        # Add hosts entry in minikube to resolve the registry hostname to LocalStack IP
        registry_hostname = "000000000000.dkr.ecr.us-east-1.localhost"
        run_command(
            [
                "minikube",
                "ssh",
                f"echo '{localstack_ip} {registry_hostname}' | sudo tee -a /etc/hosts",
            ],
            check=True,
        )

        # Restart containerd to pick up the changes
        run_command(
            ["minikube", "ssh", "sudo systemctl restart containerd"],
            check=False,
        )

        console.print("[green]✓ LocalStack registry DNS configured[/green]")
        console.print(f"  Registry hostname: {registry_hostname}:4566")
        console.print(f"  Resolves to: {localstack_ip} (LocalStack container)")

    except subprocess.CalledProcessError as e:
        console.print("[yellow]⚠ Failed to configure LocalStack registry DNS[/yellow]")
        console.print(f"[yellow]  Error: {e}[/yellow]")


def show_cluster_info() -> None:
    """Display cluster information."""
    console.print("\n📊 [bold]Cluster Information:[/bold]")

    try:
        # Get basic cluster info
        storage_result = run_command(["kubectl", "get", "storageclass", "-o", "name"])
        nodes_result = run_command(["kubectl", "get", "nodes", "--no-headers"])

        storage_count = len(
            [sc for sc in storage_result.stdout.strip().split("\n") if sc]
        )
        node_count = len([n for n in nodes_result.stdout.strip().split("\n") if n])

        # Check gVisor status
        gvisor_result = run_command(
            ["kubectl", "get", "runtimeclass", "gvisor"], check=False
        )
        gvisor_status = (
            "✅ Enabled" if gvisor_result.returncode == 0 else "❌ Not enabled"
        )

        info_text = f"""
Storage Classes: {storage_count} found
Nodes: {node_count} ready
gVisor Runtime: {gvisor_status}
Sample Compose: /tmp/test-compose.yml
"""

        # Add gVisor note if enabled
        if gvisor_status.startswith("✅"):
            info_text += """
🛡️  gVisor is enabled - all pods will run with enhanced security isolation
"""
        console.print(
            Panel(info_text.strip(), title="Environment Ready", border_style="green")
        )
    except subprocess.CalledProcessError:
        console.print("[yellow]⚠ Could not gather cluster info[/yellow]")


@app.command("up")
def start_minikube(
    memory: str = typer.Option("8192", help="Memory allocation for Minikube"),
    cpus: str = typer.Option("8", help="CPU allocation for Minikube"),
    disk_size: str = typer.Option("20gb", help="Disk size for Minikube"),
    fresh: bool = typer.Option(
        False, "--fresh", help="Delete existing cluster and start fresh"
    ),
):
    """Start Minikube development environment."""
    console.print(
        "🚀 [bold blue]Starting LazyCloud Minikube Development Environment...[/bold blue]"
    )

    # Check prerequisites
    if not check_prerequisites():
        raise typer.Exit(1)

    # Handle fresh start or start if not running
    need_to_start = fresh or not is_minikube_running()

    if fresh:
        cleanup_existing_cluster()
    elif is_minikube_running():
        console.print("[yellow]Minikube is already running[/yellow]")
        need_to_start = False

    if need_to_start:
        # Start minikube
        console.print("🔧 [bold]Starting Minikube cluster...[/bold]")
        try:
            run_command(
                [
                    "minikube",
                    "start",
                    "--network=lazycloud",
                    f"--memory={memory}",
                    f"--cpus={cpus}",
                    f"--disk-size={disk_size}",
                    "--driver=docker",
                    "--container-runtime=containerd",  # Required for gVisor
                    "--docker-opt",
                    "containerd=/var/run/containerd/containerd.sock",
                    "--insecure-registry=000000000000.dkr.ecr.us-east-1.localhost:4566",
                ]
            )
            console.print("[green]✓ Minikube cluster started successfully[/green]")
        except subprocess.CalledProcessError:
            console.print("[red]❌ Failed to start Minikube[/red]")
            raise typer.Exit(1)

    # Verify minikube is actually running before proceeding
    if not is_minikube_running():
        console.print("[red]❌ Minikube failed to start properly[/red]")
        raise typer.Exit(1)

    # Enable addons
    console.print("🔧 [bold]Enabling Minikube addons...[/bold]")
    for addon in MINIKUBE_ADDONS:
        try:
            run_command(["minikube", "addons", "enable", addon])
        except subprocess.CalledProcessError:
            console.print(f"[yellow]⚠ Failed to enable {addon} addon[/yellow]")
    console.print("[green]✓ Addons enabled[/green]")

    # Setup environment
    setup_storage_class()
    setup_test_namespace()
    verify_gvisor_runtime()
    configure_localstack_registry_dns()

    # Show results
    show_cluster_info()
    console.print("[green]✅ LazyCloud Minikube environment is ready! 🎉[/green]")


@app.command("down")
def stop_minikube(
    delete: bool = typer.Option(
        False, "--delete", help="Completely delete the cluster"
    ),
    cleanup_docker: bool = typer.Option(
        False, "--cleanup-docker", help="Clean up Docker resources"
    ),
):
    """Stop Minikube development environment."""
    console.print(
        "🛑 [bold red]Stopping LazyCloud Minikube Development Environment...[/bold red]"
    )

    if not is_minikube_running():
        console.print("[yellow]Minikube is not running[/yellow]")
        return

    # Clean up resources
    console.print("🧹 [bold]Cleaning up test resources...[/bold]")

    cleanup_commands = [
        (
            [
                "kubectl",
                "delete",
                "namespace",
                "lazycloud-test",
                "--ignore-not-found=true",
            ],
            "test namespace",
        ),
        (
            ["kubectl", "delete", "storageclass", "efs-sc", "--ignore-not-found=true"],
            "EFS storage class",
        ),
    ]

    for cmd, description in cleanup_commands:
        try:
            run_command(cmd, check=False)
            console.print(f"[green]✓ Removed {description}[/green]")
        except subprocess.CalledProcessError:
            pass

    # Remove sample file
    sample_path = Path("/tmp/test-compose.yml")
    if sample_path.exists():
        sample_path.unlink()
        console.print("[green]✓ Removed sample compose file[/green]")

    # Stop/delete minikube
    if delete:
        console.print("🗑️ [bold]Deleting Minikube cluster...[/bold]")
        run_command(["minikube", "delete"])
        console.print("[green]✓ Minikube cluster deleted completely[/green]")
    else:
        console.print("🛑 [bold]Stopping Minikube...[/bold]")
        run_command(["minikube", "stop"])
        console.print("[green]✓ Minikube stopped[/green]")
        console.print(
            "[blue]ℹ️ Cluster preserved (use --delete to remove completely)[/blue]"
        )

    # Docker cleanup
    if cleanup_docker:
        console.print("🧹 [bold]Cleaning up Docker resources...[/bold]")
        run_command(["docker", "system", "prune", "-f"])
        console.print("[green]✓ Docker cleanup completed[/green]")

    console.print("[green]✅ Cleanup completed![/green]")


@app.command("status")
def show_status():
    """Show Minikube and cluster status."""
    console.print("📊 [bold blue]LazyCloud Minikube Status[/bold blue]")

    if not shutil.which("minikube"):
        console.print("[red]❌ Minikube not installed[/red]")
        return

    if not is_minikube_running():
        console.print("[red]❌ Minikube is not running[/red]")
        console.print("[blue]💡 Run 'uv run minikube-up' to start[/blue]")
        return

    console.print("[green]✅ Minikube is running[/green]")

    try:
        # Check resources
        checks = {
            "Test Namespace": (
                ["kubectl", "get", "namespace", "lazycloud-test"],
                "lazycloud-test namespace exists",
            ),
            "EFS StorageClass": (
                ["kubectl", "get", "storageclass", "efs-sc"],
                "EFS storage class exists",
            ),
            "Sample Compose": (None, Path("/tmp/test-compose.yml").exists()),
        }

        # Get cluster info
        nodes_result = run_command(["kubectl", "get", "nodes", "--no-headers"])
        namespaces_result = run_command(["kubectl", "get", "namespaces", "-o", "name"])

        node_count = len([n for n in nodes_result.stdout.strip().split("\n") if n])
        namespace_count = len(
            [ns for ns in namespaces_result.stdout.strip().split("\n") if ns]
        )

        status_info = f"Nodes: {node_count}\nNamespaces: {namespace_count}\n"

        for name, (cmd, expected) in checks.items():
            if cmd:
                result = run_command(cmd, check=False)
                status = "✅" if result.returncode == 0 else "❌"
            else:
                status = "✅" if expected else "❌"
            status_info += f"{name}: {status}\n"

        console.print(
            Panel(status_info.strip(), title="Cluster Status", border_style="green")
        )

    except subprocess.CalledProcessError:
        console.print("[yellow]⚠️ Could not gather detailed cluster status[/yellow]")


@app.command("dashboard")
def open_dashboard():
    """Open Minikube dashboard in browser."""
    console.print("🌐 [bold blue]Opening Minikube Dashboard...[/bold blue]")

    if not is_minikube_running():
        console.print("[red]❌ Minikube is not running[/red]")
        console.print("[blue]💡 Run 'uv run minikube-up' to start[/blue]")
        return

    try:
        console.print("[green]🚀 Starting dashboard...[/green]")
        console.print("[yellow]Press Ctrl+C to stop the dashboard[/yellow]")
        subprocess.run(["minikube", "dashboard"], check=True)
    except KeyboardInterrupt:
        console.print("\n[yellow]🛑 Dashboard stopped by user[/yellow]")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]❌ Failed to start dashboard: {e}[/red]")


# Entry points for uv run commands
def help():
    """Entry point for minikube-help."""
    print("Available commands:")
    print(" - mk-up: Start Minikube (--fresh to delete existing cluster)")
    print(" - mk-down: Stop Minikube (--delete to delete cluster)")
    print(" - mk-dash: Open Minikube dashboard")
    print(" - mk-status: Show Minikube and cluster status")


def main():
    """Entry point for minikube-up."""

    fresh = "--fresh" in sys.argv
    start_minikube(memory="4096", cpus="2", disk_size="20gb", fresh=fresh)


def main_down():
    """Entry point for minikube-down."""

    delete = "--delete" in sys.argv
    cleanup_docker = "--cleanup-docker" in sys.argv
    stop_minikube(delete=delete, cleanup_docker=cleanup_docker)


def main_dashboard():
    """Entry point for minikube-dash."""
    open_dashboard()


def main_status():
    """Entry point for minikube-status."""

    # Just call the typer app with status command
    sys.argv = ["minikube_dev.py", "status"]
    app()


if __name__ == "__main__":
    app()
