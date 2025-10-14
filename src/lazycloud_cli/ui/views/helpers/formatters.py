"""Pure formatting functions for deployment UI."""

from typing import Any

DEFAULT_DISABLED_VALUE = "Disabled"


def format_resource_limits(resources: dict[str, Any] | None) -> str:
    """Format resource limits/reservations for display."""
    if not resources:
        return DEFAULT_DISABLED_VALUE

    parts = []
    if "cpus" in resources:
        parts.append(f"CPU: {resources['cpus']}")
    if "memory" in resources:
        parts.append(f"Memory: {resources['memory']}")

    return ", ".join(parts) if parts else DEFAULT_DISABLED_VALUE


def format_port_mapping(port: str | dict[str, Any] | None) -> str:
    """Format a single port mapping."""
    if not port:
        return DEFAULT_DISABLED_VALUE

    if isinstance(port, str):
        return port

    if isinstance(port, dict):
        published = port.get("published", "")
        target = port.get("target", "")
        return f"{published}:{target}"


def format_ports_list(ports: list[Any] | None, max_display: int = 3) -> str:
    """Format a list of ports for display."""
    if not ports:
        return DEFAULT_DISABLED_VALUE

    formatted = [format_port_mapping(p) for p in ports[:max_display]]
    result = ", ".join(formatted)

    if len(ports) > max_display:
        result += f" (+{len(ports) - max_display} more)"

    return result if result else DEFAULT_DISABLED_VALUE


def format_image_change(old_image: str | None, new_image: str | None) -> str:
    """Format image change, highlighting tag changes."""
    # Parse old image safely
    if old_image and ":" in old_image:
        old_base, old_tag = old_image.rsplit(":", 1)
    else:
        old_base, old_tag = old_image, None

    # Parse new image safely
    if new_image and ":" in new_image:
        new_base, new_tag = new_image.rsplit(":", 1)
    else:
        new_base, new_tag = new_image, None

    # Format output
    old_str = f"{old_base}:{old_tag}" if old_base and old_tag else (old_base or "None")
    new_str = f"{new_base}:{new_tag}" if new_base and new_tag else (new_base or "None")

    return f"{old_str} → {new_str}"


def format_scaling_config(scaling: dict[str, Any] | None) -> str:
    """Format scaling configuration for display."""
    if not scaling or not scaling.get("enabled"):
        return DEFAULT_DISABLED_VALUE

    min_replicas = scaling.get("min", 1)
    max_replicas = scaling.get("max", 10)
    cpu_target = scaling.get("cpu", 70)
    memory_target = scaling.get("memory", 70)

    return f"{min_replicas}-{max_replicas} replicas (CPU: {cpu_target}%, Memory: {memory_target}%)"


def format_deploy_config(deploy: dict[str, Any] | None) -> str:
    """Format deployment configuration summary."""
    parts = []

    if not deploy:
        return DEFAULT_DISABLED_VALUE

    if "replicas" in deploy:
        parts.append(f"replicas: {deploy['replicas']}")

    if "limits" in deploy and isinstance(deploy["limits"], dict):
        limits = format_resource_limits(deploy["limits"])
        if limits != "none":
            parts.append(f"limits: {limits}")

    if "reservations" in deploy and isinstance(deploy["reservations"], dict):
        reservations = format_resource_limits(deploy["reservations"])
        if reservations != "none":
            parts.append(f"reserved: {reservations}")

    return ", ".join(parts) if parts else "default"


def format_healthcheck(healthcheck: dict[str, Any] | None) -> str:
    """Format healthcheck configuration."""
    if not healthcheck:
        return DEFAULT_DISABLED_VALUE

    test = healthcheck.get("test")
    if not test:
        return "configured"

    if isinstance(test, list):
        test = " ".join(test)

    # Truncate long commands
    if len(test) > 40:
        test = test[:37] + "..."

    return test


def format_value_summary(value: Any, max_length: int = 50) -> str:
    """Format any value for concise display."""
    if value is None:
        return DEFAULT_DISABLED_VALUE

    if isinstance(value, bool):
        return "enabled" if value else "disabled"

    if isinstance(value, dict):
        if "enabled" in value:  # Scaling config
            return format_scaling_config(value)
        elif "replicas" in value or "limits" in value:  # Deploy config
            return format_deploy_config(value)
        elif len(value) <= 3:
            items = [f"{k}: {v}" for k, v in value.items()]
            return "{" + ", ".join(items) + "}"
        else:
            return f"{len(value)} items"

    if isinstance(value, list):
        if not value:
            return DEFAULT_DISABLED_VALUE

        elif len(value) <= 3:
            return "[" + ", ".join(str(v) for v in value) + "]"
        else:
            return f"{len(value)} items"

    result = str(value)
    if len(result) > max_length:
        return result[: max_length - 3] + "..."

    return result


def format_env_var_list(env_vars: dict[str, str] | list[str] | None) -> str:
    """Format environment variables for display."""
    if not env_vars:
        return DEFAULT_DISABLED_VALUE

    if isinstance(env_vars, (dict, list)):
        return f"{len(env_vars)} variables"


def format_volume_list(volumes: list[Any] | None) -> str:
    """Format volume list for display."""
    if not volumes:
        return DEFAULT_DISABLED_VALUE

    if len(volumes) == 1:
        return str(volumes[0])
    elif len(volumes) <= 3:
        return ", ".join(str(v) for v in volumes)
    else:
        return f"{', '.join(str(v) for v in volumes[:2])} (+{len(volumes) - 2} more)"


def format_command(command: str | list[str] | None, max_length: int = 40) -> str:
    """Format command for display."""
    if not command:
        return DEFAULT_DISABLED_VALUE

    if isinstance(command, list):
        command = " ".join(command)

    if len(command) > max_length:
        return command[: max_length - 3] + "..."

    return command


def truncate_text(text: str | None, max_length: int = 50) -> str:
    """Truncate text to maximum length with ellipsis."""
    if not text:
        return DEFAULT_DISABLED_VALUE

    if len(text) <= max_length:
        return text

    return text[: max_length - 3] + "..."
