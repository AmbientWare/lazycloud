from typing import Dict, List
from pydantic import BaseModel

from lazycloud_cli.api.base import BaseAPI


class MachineOptions(BaseModel):
    regions: List[str]
    options: Dict[str, List[int]]


class VolumesAPI(BaseAPI):
    def __init__(self):
        super().__init__("volumes")

    def extend_volume(
        self,
        machine_id: int,
        size: int,
    ) -> None:
        """Extend a volume for a machine"""

        def _extend():
            return self._put(json={"machine_id": machine_id, "size": size})

        return self._run_with_spinner("Extending volume...", _extend)


volumes_api = VolumesAPI()
