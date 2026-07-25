"""Pinned Tailscale install artifact consumed by every machine bootstrap owner."""

TAILSCALE_INSTALL_VERSION = "1.98.8"
TAILSCALE_AMD64_SHA256 = "3a55b5900dd7e11e09b6c74d1e46d223d549dfbefbdc1f044a8ab7bdbafb933c"
TAILSCALE_ARM64_SHA256 = "53eb3ce89d062fd34e393d24a6c8ec08c769fede8eb77fe9c6e347ad4ae00f84"

__all__ = [
    "TAILSCALE_AMD64_SHA256",
    "TAILSCALE_ARM64_SHA256",
    "TAILSCALE_INSTALL_VERSION",
]
