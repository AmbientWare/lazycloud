import re


def convert_cpu_value(cpu_value: str | float | int) -> str:
    """Convert Docker Compose CPU value to Kubernetes format."""
    if isinstance(cpu_value, str):
        # Already in string format, return as-is
        return cpu_value
    else:
        # Convert number to string (e.g., 1.5 -> "1.5")
        return str(cpu_value)


def convert_memory_value(memory_value: str | int) -> str:
    """Convert Docker Compose memory value to Kubernetes format."""
    if isinstance(memory_value, str):
        # Check if already in Kubernetes format (Gi, Mi, Ki)
        if memory_value.endswith(("Gi", "Mi", "Ki", "Ti")):
            return memory_value  # Already in correct format

        # Convert common Docker formats to Kubernetes formats
        memory_str = memory_value.upper()
        # Docker uses 'G' for GB, K8s prefers 'Gi' for GiB
        if memory_str.endswith("G"):
            return memory_str[:-1] + "Gi"  # Replace G with Gi

        elif memory_str.endswith("M"):
            return memory_str[:-1] + "Mi"  # Replace M with Mi

        elif memory_str.endswith("K"):
            return memory_str[:-1] + "Ki"  # Replace K with Ki

        else:
            # Already in proper format or bytes
            return memory_str
    else:
        # Assume int - bytes, convert to appropriate unit
        if isinstance(memory_value, int):
            if memory_value >= 1024**3:
                return f"{memory_value // (1024**3)}Gi"
            elif memory_value >= 1024**2:
                return f"{memory_value // (1024**2)}Mi"
            elif memory_value >= 1024:
                return f"{memory_value // 1024}Ki"
            else:
                return f"{memory_value}"
        else:
            return str(memory_value)


def parse_duration(duration_str: str) -> int:
    """Parse Docker duration string to seconds."""
    if not duration_str:
        return 0

    try:
        if duration_str.endswith("s"):
            return int(duration_str[:-1])

        elif duration_str.endswith("m"):
            return int(duration_str[:-1]) * 60

        elif duration_str.endswith("h"):
            return int(duration_str[:-1]) * 3600

        else:
            return int(duration_str)

    except ValueError:
        return 0


def is_kubernetes_name_compliant(name: str, max_length: int = 63) -> bool:
    """
    Check if a name is Kubernetes DNS-1123 compliant.
    """
    if not name or len(name) > max_length:
        return False

    # Check if it matches the DNS-1123 pattern
    # Must be lowercase alphanumeric with hyphens, start/end with letter
    pattern = r"^[a-z]([a-z0-9-]*[a-z0-9])?$"
    return bool(re.match(pattern, name))


def sanitize_name(name: str, max_length: int = 63) -> str:
    """
    Sanitize names for Kubernetes (DNS-1123 compliant).
    """
    # If empty, return default
    if not name:
        return "default"

    # Convert to lowercase and replace invalid characters with hyphens
    sanitized = re.sub(r"[^a-z0-9\-]", "-", name.lower())

    # Remove leading/trailing hyphens
    sanitized = sanitized.strip("-")

    # If empty after sanitization, use default
    if not sanitized:
        return "default"

    # Handle names starting with digits
    if sanitized[0].isdigit():
        sanitized = f"n{sanitized}"

    # Truncate if too long
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length].rstrip("-")

    return sanitized
