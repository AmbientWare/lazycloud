from shared.enums import StringEnum


class ReserveMachineState(StringEnum):
    Serving = "serving"
    Starting = "starting"
    Draining = "draining"
    Preparing = "preparing"
    Stopping = "stopping"
    Unavailable = "unavailable"
    Failed = "failed"
    Terminating = "terminating"
    Stopped = "stopped"
    HibernateUnverified = "hibernate_unverified"
    ImageSaved = "image_saved"

    @property
    def stopped(self) -> bool:
        return self in {self.Stopped, self.HibernateUnverified, self.ImageSaved}

    @property
    def reserve(self) -> bool:
        return self in {self.Preparing, self.Stopping} or self.stopped
