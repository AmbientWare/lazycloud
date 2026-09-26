from shared.enums import StringEnum


class CapacitySleepMode(StringEnum):
    Stop = "stop"
    Hibernate = "hibernate"


class CapacitySleepOutcome(StringEnum):
    Unknown = "unknown"
    Stopped = "stopped"
    Hibernated = "hibernated"


class CapacityActivationKind(StringEnum):
    Provision = "provision"
    Boot = "boot"
    Resume = "resume"
