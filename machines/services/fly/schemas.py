from enum import Enum
from pydantic import BaseModel

from machines.services.platform.schemas import ImageTypes


class FlyCommandError(Exception):
    """Raised when a Fly.io command fails."""

    pass


class FlyRegion(Enum):
    AMS = "ams"  # Amsterdam, Netherlands
    CDG = "cdg"  # Paris, France
    DEN = "den"  # Denver, Colorado (US)
    DFW = "dfw"  # Dallas, Texas (US)
    FRA = "fra"  # Frankfurt, Germany
    HKG = "hkg"  # Hong Kong, Hong Kong
    IAD = "iad"  # Ashburn, Virginia (US)
    LAX = "lax"  # Los Angeles, California (US)
    LHR = "lhr"  # London, United Kingdom
    NRT = "nrt"  # Tokyo, Japan
    ORD = "ord"  # Chicago, Illinois (US)
    SCL = "scl"  # Santiago, Chile
    SEA = "sea"  # Seattle, Washington (US)
    SIN = "sin"  # Singapore, Singapore
    SJC = "sjc"  # San Jose, California (US)
    SYD = "syd"  # Sydney, Australia


class FlyMachineConfig(BaseModel):
    machine_id: int
    usage_uuid: str
    region: FlyRegion = FlyRegion.LAX
    cpu_kind: str = "shared"
    cpu: int = 1
    memory: int = 1024
    file_system_id: int
    gpu_kind: str | None = None
    image_type: ImageTypes = ImageTypes.UBUNTU_22_04
    port: int
    public_key: str


RESOURCE_MAP = {
    "shared": {
        1: {"name": "shared-cpu-1x", "memory_options": [256, 512, 1024, 2048]},
        2: {"name": "shared-cpu-2x", "memory_options": [512, 1024, 2048, 4096]},
        4: {"name": "shared-cpu-4x", "memory_options": [1024, 2048, 4096, 8192]},
        8: {"name": "shared-cpu-8x", "memory_options": [2048, 4096, 8192, 16384]},
    },
    "performance": {
        1: {"name": "performance-1x", "memory_options": [2048, 4096, 8192]},
        2: {"name": "performance-2x", "memory_options": [4096, 8192, 16384]},
        4: {"name": "performance-4x", "memory_options": [8192, 16384, 32768]},
        8: {"name": "performance-8x", "memory_options": [16384, 32768, 65536]},
        16: {"name": "performance-16x", "memory_options": [32768, 65536, 131072]},
    },
    "gpus": {
        "a10": {"regions": [FlyRegion.ORD]},
        "l40s": {"regions": [FlyRegion.ORD]},
        "a100-40gb": {"regions": [FlyRegion.ORD]},
        "a100-80gb": {"regions": [FlyRegion.IAD, FlyRegion.SJC, FlyRegion.SYD]},
    },
}
