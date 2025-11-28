import subprocess
import sys
import tempfile
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
from minikube.utils import (
    check_prerequisites,
    is_minikube_running,
    retry_until,
    run_command,
)

console = Console()
app = typer.Typer(help="Manage LazyCloud Minikube development environment")


def ensure_minikube_on_lazycloud_network() -> bool:
    """Ensure minikube is connected to lazycloud network."""
    try:
        result = run_command(
            ["docker", "ps", "--filter", "name=minikube", "--format", "{{.Names}}"],
            check=False,
        )
        container_name = (
            result.stdout.strip().split("\n")[0] if result.stdout.strip() else None
        )
        if not container_name:
            return False
        run_command(
            ["docker", "network", "connect", "lazycloud", container_name], check=False
        )
        return True
    except Exception:
        return False


def create_docker_kubeconfig() -> None:
    """Create a Docker-specific kubeconfig file with minikube:8443 and embedded certificates."""
    try:
        home = Path.home()
        kubeconfig_dir = home / ".kube"
        kubeconfig_dir.mkdir(exist_ok=True)
        docker_kubeconfig = kubeconfig_dir / "config-docker"
        minikube_dir = home / ".minikube"
        profiles_dir = minikube_dir / "profiles" / "minikube"

        # Read certificate files
        ca_crt_path = minikube_dir / "ca.crt"
        client_crt_path = profiles_dir / "client.crt"
        client_key_path = profiles_dir / "client.key"

        if not all(
            [ca_crt_path.exists(), client_crt_path.exists(), client_key_path.exists()]
        ):
            console.print(
                "[yellow]⚠ Certificate files not found, cannot create Docker kubeconfig[/yellow]"
            )
            return

        # Create temporary directory for certificate files
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_ca = Path(temp_dir) / "ca.crt"
            temp_client_crt = Path(temp_dir) / "client.crt"
            temp_client_key = Path(temp_dir) / "client.key"

            # Copy certificates to temp directory
            temp_ca.write_bytes(ca_crt_path.read_bytes())
            temp_client_crt.write_bytes(client_crt_path.read_bytes())
            temp_client_key.write_bytes(client_key_path.read_bytes())

            # Create a fresh minimal kubeconfig for Docker (don't copy main config)
            # This ensures it's not affected by other clusters or minikube updates
            if docker_kubeconfig.exists():
                docker_kubeconfig.unlink()

            # Set cluster with minikube:8443 and embedded certificates
            run_command(
                [
                    "kubectl",
                    "config",
                    "set-cluster",
                    "minikube",
                    "--server=https://minikube:8443",
                    f"--certificate-authority={temp_ca}",
                    "--embed-certs=true",
                    f"--kubeconfig={docker_kubeconfig}",
                ],
                check=True,
            )

            # Set user credentials with embedded client certificates
            run_command(
                [
                    "kubectl",
                    "config",
                    "set-credentials",
                    "minikube",
                    f"--client-certificate={temp_client_crt}",
                    f"--client-key={temp_client_key}",
                    "--embed-certs=true",
                    f"--kubeconfig={docker_kubeconfig}",
                ],
                check=True,
            )

            # Create context linking cluster and user
            run_command(
                [
                    "kubectl",
                    "config",
                    "set-context",
                    "minikube",
                    "--cluster=minikube",
                    "--user=minikube",
                    "--namespace=default",
                    f"--kubeconfig={docker_kubeconfig}",
                ],
                check=True,
            )

            # Set minikube as current context
            run_command(
                [
                    "kubectl",
                    "config",
                    "use-context",
                    "minikube",
                    f"--kubeconfig={docker_kubeconfig}",
                ],
                check=True,
            )

        console.print(
            f"[green]✓ Created Docker kubeconfig at {docker_kubeconfig}[/green]"
        )

    except Exception as e:
        console.print(f"[yellow]⚠ Could not create Docker kubeconfig: {e}[/yellow]")


@app.command("up")
def start_minikube(
    memory: str = typer.Option("16384", help="Memory allocation for Minikube (MB)"),
    cpus: str = typer.Option("12", help="CPU allocation for Minikube"),
    disk_size: str = typer.Option("20gb", help="Disk size for Minikube"),
    fresh: bool = typer.Option(
        False, "--fresh", help="Delete existing cluster and start fresh"
    ),
    monitoring: bool = typer.Option(
        True,
        "--monitoring/--no-monitoring",
        help="Install monitoring stack (Prometheus + Grafana)",
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
        console.print("🔧 [bold]Starting Minikube cluster...[/bold]")
        console.print(
            "[dim]This may take a few minutes. Minikube output will be shown below...[/dim]"
        )
        try:
            subprocess.run(
                [
                    "minikube",
                    "start",
                    f"--memory={memory}",
                    f"--cpus={cpus}",
                    f"--disk-size={disk_size}",
                    "--driver=docker",
                    "--container-runtime=containerd",  # Required for gVisor
                    "--docker-opt",
                    "containerd=/var/run/containerd/containerd.sock",
                    "--insecure-registry=000000000000.dkr.ecr.us-east-1.localhost:4566",
                ],
                check=True,
            )
            console.print("[green]✓ Minikube cluster started successfully[/green]")

        except subprocess.CalledProcessError:
            console.print("[red]❌ Failed to start Minikube[/red]")
            raise typer.Exit(1)

    # Verify minikube is ready
    console.print("🔍 [bold]Verifying Minikube is ready...[/bold]")
    if retry_until(is_minikube_running, description="Minikube to be ready"):
        console.print("[green]✓ Minikube is running and ready[/green]")
    else:
        console.print("[red]❌ Minikube failed to start properly after waiting[/red]")
        raise typer.Exit(1)

    # Connect minikube to lazycloud network (for Docker Compose services to access it)
    console.print("🔧 [bold]Connecting Minikube to lazycloud network...[/bold]")
    if ensure_minikube_on_lazycloud_network():
        console.print("[green]✓ Minikube connected to lazycloud network[/green]")
    else:
        console.print(
            "[yellow]⚠ Could not connect minikube to lazycloud network[/yellow]"
        )

    # All operations below run from HOST and need IP-based kubeconfig
    # Only update kubeconfig to minikube:8443 AFTER all host-side operations complete

    # Enable addons (host-side: minikube addons enable)
    console.print("🔧 [bold]Enabling Minikube addons...[/bold]")
    for addon in MINIKUBE_ADDONS:
        try:
            run_command(["minikube", "addons", "enable", addon])
        except subprocess.CalledProcessError:
            console.print(f"[yellow]⚠ Failed to enable {addon} addon[/yellow]")
    console.print("[green]✓ Addons enabled[/green]")

    # Setup environment (all use kubectl from host)
    setup_storage_class()  # kubectl apply
    setup_test_namespace()  # kubectl create
    verify_gvisor_runtime()  # kubectl get
    ensure_localstack_running()  # docker commands only
    configure_localstack_registry_dns()  # minikube ssh + docker inspect
    if monitoring:
        setup_monitoring_stack()  # helm + kubectl

    # Show cluster info (uses kubectl from host)
    show_cluster_info()

    # Create Docker-specific kubeconfig (containers can resolve minikube via Docker DNS)
    console.print("🔧 [bold]Creating Docker-specific kubeconfig...[/bold]")
    create_docker_kubeconfig()

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
    """Entry point for mk-up."""
    sys.argv = ["minikube_dev.py", "up"] + sys.argv[1:]
    app()


def main_down():
    """Entry point for mk-down."""
    sys.argv = ["minikube_dev.py", "down"] + sys.argv[1:]
    app()


def main_dashboard():
    """Entry point for mk-dash."""
    sys.argv = ["minikube_dev.py", "dashboard"]
    app()


def main_status():
    """Entry point for mk-status."""
    sys.argv = ["minikube_dev.py", "status"]
    app()


if __name__ == "__main__":
    app()
