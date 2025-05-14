from lazycloud_api.services.fly.api.apps import AppsAPI
from lazycloud_api.services.fly.api.machines import MachinesAPI
from lazycloud_api.services.fly.api.volumes import VolumesAPI


class FlyAPI:
    def __init__(self):
        self.apps = AppsAPI()
        self.machines = MachinesAPI()
        self.volumes = VolumesAPI()


fly_api = FlyAPI()

__all__ = ["fly_api"]
