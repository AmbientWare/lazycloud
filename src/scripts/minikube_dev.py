import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from minikube.constants import MINIKUBE_ADDONS, PROMETHEUS_NAMESPACE
from minikube.operations import (
    cleanup_existing_cluster,
    configure_localstack_registry_dns,
    ensure_localstack_running,
    setup_monitoring_stack,
    setup_storage_class,
    setup_test_namespace,
    show_cluster_info,
    verify_gvisor_runtime,
)
from minikube.utils import check_prerequisites, is_minikube_running, run_command

console = Console()
app = typer.Typer(help="Manage LazyCloud Minikube development environment")


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

            # Create Docker-friendly kubeconfig with embedded certificates
            console.print("🔧 [bold]Creating Docker-friendly kubeconfig...[/bold]")
            try:
                kube_dir = Path.home() / ".kube"
                kube_dir.mkdir(exist_ok=True)

                # Write to config-docker file
                result = subprocess.run(
                    ["kubectl", "config", "view", "--flatten", "--minify"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                (kube_dir / "config-docker").write_text(result.stdout)
                console.print(
                    "[green]✓ Docker-friendly kubeconfig created at ~/.kube/config-docker[/green]"
                )
            except Exception as e:
                console.print(
                    f"[yellow]⚠ Could not create Docker kubeconfig: {e}[/yellow]"
                )

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
    ensure_localstack_running()
    configure_localstack_registry_dns()
    setup_monitoring_stack()

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

    # Optionally clean up monitoring stack
    if delete:
        console.print("🧹 [bold]Cleaning up monitoring stack...[/bold]")
        cleanup_commands.extend(
            [
                (
                    ["helm", "uninstall", "prometheus", "-n", PROMETHEUS_NAMESPACE],
                    "Prometheus stack",
                ),
                (
                    [
                        "kubectl",
                        "delete",
                        "namespace",
                        PROMETHEUS_NAMESPACE,
                        "--ignore-not-found=true",
                    ],
                    "monitoring namespace",
                ),
            ]
        )

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

    if not check_prerequisites():
        console.print("[red]❌ Required tools not installed[/red]")
        return

    if not is_minikube_running():
        console.print("[red]❌ Minikube is not running[/red]")
        console.print("[blue]💡 Run 'mk-up' to start[/blue]")
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
        console.print("[blue]💡 Run 'mk-up' to start[/blue]")
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
    print(
        " - mk-up: Start Minikube with monitoring (--fresh to delete existing cluster)"
    )
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
