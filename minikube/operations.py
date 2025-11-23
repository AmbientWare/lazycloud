import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from minikube.constants import (
    EFS_STORAGE_CLASS_YAML,
    PROMETHEUS_CHART,
    PROMETHEUS_HELM_REPO,
    PROMETHEUS_HELM_REPO_URL,
    PROMETHEUS_NAMESPACE,
    S3_STORAGE_CLASS_YAML,
)
from minikube.utils import is_minikube_running, run_command

console = Console()


def cleanup_existing_cluster() -> None:
    """Clean up existing minikube cluster."""
    console.print("🗑️ [bold yellow]Deleting existing cluster...[/bold yellow]")
    try:
        if is_minikube_running():
            console.print("🛑 [bold]Stopping existing cluster...[/bold]")
            run_command(["minikube", "stop"], check=False)

        # Delete with --purge to remove all profiles and cached data
        run_command(["minikube", "delete", "--all", "--purge"], check=False)

        # Force cleanup of any stale minikube containers on the lazycloud network
        console.print("🧹 [bold]Cleaning up network connections...[/bold]")
        result = run_command(
            ["docker", "ps", "-aq", "--filter", "name=minikube"], check=False
        )
        if result.stdout.strip():
            container_ids = result.stdout.strip().split("\n")
            for container_id in container_ids:
                run_command(["docker", "rm", "-f", container_id], check=False)

        # Also remove minikube config directory to clear cached IP assignments
        minikube_config = Path.home() / ".minikube"
        if minikube_config.exists():
            shutil.rmtree(minikube_config, ignore_errors=True)
            console.print("  Cleared minikube configuration cache")

        # Wait a moment for Docker to release IPs
        time.sleep(3)

        console.print("[green]✓ Existing cluster deleted[/green]")
    except subprocess.CalledProcessError:
        console.print("[yellow]⚠ Cluster deletion had issues (continuing)[/yellow]")


def setup_storage_class() -> None:
    """Create EFS-compatible StorageClass."""
    console.print("🔧 [bold]Creating EFS-compatible StorageClass...[/bold]")

    storage_classes = {
        "efs": EFS_STORAGE_CLASS_YAML,
        "s3": S3_STORAGE_CLASS_YAML,
    }

    for name, storage_class in storage_classes.items():
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(storage_class)
            temp_file = f.name

        try:
            run_command(["kubectl", "apply", "-f", temp_file])
            console.print(f"[green]✓ {name} StorageClass created[/green]")
        except subprocess.CalledProcessError:
            console.print(
                f"[yellow]⚠ Failed to create {name} storage class (continuing)[/yellow]"
            )
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


def ensure_localstack_running() -> None:
    """Ensure LocalStack container is running before configuring DNS."""
    console.print("🔧 [bold]Ensuring LocalStack is running...[/bold]")

    # Check if container exists and is running
    check_result = run_command(
        [
            "docker",
            "ps",
            "--filter",
            "name=lazycloud-localstack",
            "--format",
            "{{.Names}}",
        ],
        check=False,
    )

    if check_result.stdout.strip():
        console.print("[green]✓ LocalStack is already running[/green]")
        return

    # Check if container exists but is stopped
    check_stopped = run_command(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "name=lazycloud-localstack",
            "--format",
            "{{.Names}}",
        ],
        check=False,
    )

    if check_stopped.stdout.strip():
        console.print("  Starting existing LocalStack container...")
        run_command(["docker", "start", "lazycloud-localstack"], check=False)
    else:
        # Start LocalStack via docker compose
        console.print("  Starting LocalStack via docker compose...")
        compose_file = Path.cwd() / "docker-compose.yml"
        if not compose_file.exists():
            console.print(
                "[yellow]⚠ docker-compose.yml not found, skipping LocalStack startup[/yellow]"
            )
            console.print(
                "[blue]💡 Start LocalStack manually with: docker compose up -d localstack[/blue]"
            )
            return

        run_command(
            ["docker", "compose", "up", "-d", "localstack"],
            check=False,
        )

    # Wait for LocalStack to be healthy
    console.print("  Waiting for LocalStack to be healthy...")
    max_attempts = 30
    for attempt in range(max_attempts):
        health_result = run_command(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Health.Status}}",
                "lazycloud-localstack",
            ],
            check=False,
        )

        if health_result.returncode == 0:
            status = health_result.stdout.strip()
            if status == "healthy":
                console.print("[green]✓ LocalStack is healthy[/green]")
                return
            elif status == "starting":
                time.sleep(2)
                continue

        # Fallback: check if container is running
        running_check = run_command(
            [
                "docker",
                "ps",
                "--filter",
                "name=lazycloud-localstack",
                "--format",
                "{{.Names}}",
            ],
            check=False,
        )
        if running_check.stdout.strip():
            console.print(
                "[yellow]⚠ LocalStack is running but healthcheck status unclear[/yellow]"
            )
            console.print("[blue]💡 Continuing anyway...[/blue]")
            return

        time.sleep(2)

    console.print("[yellow]⚠ LocalStack did not become healthy within timeout[/yellow]")
    console.print("[blue]💡 Continuing anyway, DNS configuration may fail[/blue]")


def configure_localstack_registry_dns() -> None:
    """Configure minikube to resolve LocalStack registry hostname.

    Uses Docker network alias - LocalStack is accessible via the alias on the
    shared 'lazycloud' network. Minikube node and pods can resolve it via Docker's DNS.
    We add an /etc/hosts entry in the minikube node for reliability.
    """
    console.print("🔧 [bold]Configuring LocalStack registry DNS...[/bold]")

    try:
        # Verify LocalStack is accessible on the network
        registry_hostname = "000000000000.dkr.ecr.us-east-1.localhost"

        # Test DNS resolution from minikube node
        result = run_command(
            [
                "minikube",
                "ssh",
                f"getent hosts {registry_hostname} || echo 'DNS resolution failed'",
            ],
            check=False,
        )

        if "DNS resolution failed" in result.stdout or result.returncode != 0:
            # DNS not working, add manual entry
            console.print(
                "  Docker DNS resolution failed, adding manual /etc/hosts entry..."
            )

            # Get LocalStack container IP on the lazycloud network
            ip_result = run_command(
                [
                    "docker",
                    "inspect",
                    "lazycloud-localstack",
                    "--format={{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                ],
                check=False,
            )

            if ip_result.returncode != 0:
                console.print(
                    "[yellow]⚠ Could not inspect LocalStack container[/yellow]"
                )
                console.print(
                    "[blue]💡 Make sure LocalStack is running: docker compose up -d localstack[/blue]"
                )
                return

            localstack_ip = ip_result.stdout.strip()

            if not localstack_ip:
                console.print(
                    "[yellow]⚠ Could not find LocalStack container IP[/yellow]"
                )
                return

            console.print(f"  LocalStack IP: {localstack_ip}")

            # Add hosts entry
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

            console.print(
                "[green]✓ LocalStack registry DNS configured (manual /etc/hosts)[/green]"
            )
        else:
            console.print(
                "[green]✓ LocalStack registry DNS working (via Docker network)[/green]"
            )

        console.print(f"  Registry: {registry_hostname}:4566")

    except subprocess.CalledProcessError as e:
        console.print("[yellow]⚠ Failed to configure LocalStack registry DNS[/yellow]")
        console.print(f"[yellow]  Error: {e}[/yellow]")


def setup_monitoring_stack() -> None:
    """Install kube-prometheus-stack via Helm."""
    console.print(
        "📊 [bold]Installing monitoring stack (Prometheus + Grafana)...[/bold]"
    )

    try:
        # Add Helm repository
        console.print("  Adding Helm repository...")
        run_command(
            ["helm", "repo", "add", PROMETHEUS_HELM_REPO, PROMETHEUS_HELM_REPO_URL],
            check=False,  # May already exist
        )
        run_command(["helm", "repo", "update"])

        # Create monitoring namespace
        console.print("  Creating monitoring namespace...")
        result = run_command(
            ["kubectl", "create", "namespace", PROMETHEUS_NAMESPACE], check=False
        )
        if result.returncode != 0 and "already exists" not in result.stderr:
            console.print(
                "[yellow]⚠ Namespace creation had issues (continuing)[/yellow]"
            )

        # Check if already installed
        check_result = run_command(
            [
                "helm",
                "list",
                "-n",
                PROMETHEUS_NAMESPACE,
                "-q",
                "--filter",
                "^prometheus$",
            ],
            check=False,
        )

        if check_result.stdout.strip():
            console.print(
                "[yellow]✓ Prometheus stack already installed, skipping[/yellow]"
            )
            return

        # Install kube-prometheus-stack
        console.print(
            "  Installing kube-prometheus-stack (this may take a few minutes)..."
        )
        run_command(
            [
                "helm",
                "install",
                "prometheus",
                f"{PROMETHEUS_HELM_REPO}/{PROMETHEUS_CHART}",
                "--namespace",
                PROMETHEUS_NAMESPACE,
                "--set",
                "prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false",
                "--set",
                "prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false",
                "--set",
                "grafana.enabled=true",
                "--set",
                "prometheus.service.type=NodePort",
                "--set",
                "prometheus.service.nodePort=30090",
                "--set",
                "prometheus.prometheusSpec.retention=90d",
                "--set",
                "prometheus.prometheusSpec.retentionSize=50GB",
                "--set-json",
                'kube-state-metrics.metricLabelsAllowlist=["pods=[lazycloud.io/service,lazycloud.io/deployment-id,lazycloud.io/workspace-id,app.kubernetes.io/instance]"]',
                "--wait",
                "--timeout=10m",
            ]
        )

        console.print("[green]✓ Monitoring stack installed successfully[/green]")

        # Show access instructions
        console.print("\n[blue]Access instructions:[/blue]")
        console.print("  Prometheus (NodePort): $(minikube ip):30090")
        console.print(
            "  Prometheus (Port-forward): kubectl port-forward -n monitoring svc/prometheus-kube-prometheus-prometheus 9090:9090"
        )
        console.print(
            "  Grafana: kubectl port-forward -n monitoring svc/prometheus-grafana 3000:80"
        )
        console.print("  Grafana default credentials: admin / prom-operator")

    except subprocess.CalledProcessError as e:
        console.print("[yellow]⚠ Failed to install monitoring stack[/yellow]")
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

        # Check monitoring stack
        monitoring_result = run_command(
            ["kubectl", "get", "namespace", PROMETHEUS_NAMESPACE], check=False
        )
        monitoring_status = (
            "✅ Installed" if monitoring_result.returncode == 0 else "❌ Not installed"
        )

        info_text = f"""
Storage Classes: {storage_count} found
Nodes: {node_count} ready
gVisor Runtime: {gvisor_status}
Monitoring Stack: {monitoring_status}
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
