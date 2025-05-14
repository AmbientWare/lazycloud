from typing import Dict, List
from pydantic import BaseModel

from lazycloud_cli.api.base import BaseAPI

class GPUInfo(BaseModel):
    regions: List[str]


class PlatformOptions(BaseModel):
    regions: List[str]
    compute: Dict[str, List[int]]
    gpu: Dict[str, GPUInfo]


class PlatformAPI(BaseAPI):
    def __init__(self):
        super().__init__("platform")

    def get_platform_options(self) -> PlatformOptions:
        """Get the options for a machine"""
        res = self._get("options")
        return PlatformOptions(
            regions=res.get("regions", []),
            compute=res.get("compute", {}),
            gpu=res.get("gpu", {}),
        )


platform_api = PlatformAPI()
