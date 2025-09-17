from shared.models.compose import (
    ComposePort,
    ComposeService,
)
from shared.models.helm import (
    HPABehavior,
    HPAMetric,
    HPAScalingPolicy,
    HPAValues,
    MetricsValues,
)


def generate_metrics_values(ports: ComposePort) -> MetricsValues | None:
    """Generate metrics/monitoring annotations from service labels."""

    metrics_port = "8080"
    metrics_path = "/metrics"

    # Verify the metrics port exists in service ports
    if ports:
        port_numbers = []
        for port in ports:
            if isinstance(port, str):
                if ":" in port:
                    port_numbers.append(port.split(":")[1].split("/")[0])

                else:
                    port_numbers.append(port.split("/")[0])

            elif port.get("target"):
                port_numbers.append(str(port["target"]))

        if metrics_port not in port_numbers:
            # Add warning or use first available port
            if port_numbers:
                metrics_port = port_numbers[0]

    return MetricsValues(
        enabled=True,
        port=metrics_port,
        path=metrics_path,
        annotations={
            "prometheus.io/scrape": "true",
            "prometheus.io/port": metrics_port,
            "prometheus.io/path": metrics_path,
        },
    )


def generate_hpa_values(service: ComposeService) -> HPAValues | None:
    """Generate auto-scaling (HPA) configuration from service labels."""
    # Check if scaling is enabled
    if not service.scaling or not service.scaling.enabled:
        return None

    try:
        min_replicas = service.scaling.min
        max_replicas = service.scaling.max

    except ValueError:
        min_replicas, max_replicas = 1, 10

    hpa_config = HPAValues(
        enabled=True,
        minReplicas=min_replicas,
        maxReplicas=max_replicas,
        metrics=[],
    )

    # CPU target percentage (default 70%)
    cpu_target = service.scaling.cpu
    if cpu_target:
        try:
            cpu_util = int(cpu_target)
            hpa_config.metrics.append(
                HPAMetric(
                    type="Resource",
                    resource={
                        "name": "cpu",
                        "target": {
                            "type": "Utilization",
                            "averageUtilization": cpu_util,
                        },
                    },
                )
            )
        except ValueError:
            pass  # Skip invalid CPU target

    # Memory target percentage (optional)
    memory_target = service.scaling.memory
    if memory_target:
        try:
            memory_util = int(memory_target)
            hpa_config.metrics.append(
                HPAMetric(
                    type="Resource",
                    resource={
                        "name": "memory",
                        "target": {
                            "type": "Utilization",
                            "averageUtilization": memory_util,
                        },
                    },
                )
            )
        except ValueError:
            pass  # Skip invalid memory target

    hpa_config.behavior = {
        "scaleUp": get_scale_policy(service.scaling.scale_up_policy, "up"),
        "scaleDown": get_scale_policy(service.scaling.scale_down_policy, "down"),
    }

    return hpa_config


def get_scale_policy(policy: str, direction: str) -> HPABehavior:
    """Get scaling policy configuration."""
    if policy == "slow":
        return HPABehavior(
            stabilizationWindowSeconds=300 if direction == "down" else 60,
            policies=[
                HPAScalingPolicy(
                    type="Percent",
                    value=10 if direction == "down" else 25,
                    periodSeconds=60,
                )
            ],
        )
    elif policy == "fast":
        return HPABehavior(
            stabilizationWindowSeconds=60 if direction == "down" else 30,
            policies=[
                HPAScalingPolicy(
                    type="Percent",
                    value=50 if direction == "down" else 100,
                    periodSeconds=30,
                )
            ],
        )

    else:  # conservative
        return HPABehavior(
            stabilizationWindowSeconds=600 if direction == "down" else 120,
            policies=[
                HPAScalingPolicy(
                    type="Percent",
                    value=5 if direction == "down" else 15,
                    periodSeconds=120,
                )
            ],
        )
