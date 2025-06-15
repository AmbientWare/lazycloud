from enum import StrEnum


class MachineStatus(StrEnum):
    """Status of a machine"""

    INITIALIZING = "Initializing"
    INITIALIZED = "Initialized"
    NETWORKING = "Setting up network"
    BUILDING = "Building machine"
    VOLUME = "Creating file system"
    VM_CREATING = "Creating virtual machine"
    DEPLOYED = "Deployed"
    DELETING = "Deleting"


class FlyRegion(StrEnum):
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
    MIA = "mia"  # Miami, Florida (US)
