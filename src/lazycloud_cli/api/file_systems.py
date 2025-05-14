from typing import Dict, List, Any, Optional
from pydantic import BaseModel
import typer

from lazycloud_cli.logging import logger
from lazycloud_cli.api.base import BaseAPI


class MachineOptions(BaseModel):
    regions: List[str]
    options: Dict[str, List[int]]


class FileSystemAPI(BaseAPI):
    def __init__(self):
        super().__init__("file-systems")

    def _get_file_system_id(self, name: str) -> int:
        """Get the file system ID for a file system"""
        res = self._get(params={"name": name})
        if not res:
            logger.error(f"File system {name} not found")
            raise typer.Exit(1)

        return res[0].get("id")

    def list_file_systems(self) -> List[Dict]:
        """List all file systems"""
        try:
            res = self._get()

            return res

        except Exception as e:
            logger.error(f"Error listing file systems: {e}")
            return []

    def get_available_file_systems(self) -> List[Dict]:
        """Get available file systems"""
        res = self._get(params={"available": True})
        return res

    def get_file_system(self, file_system_name: str) -> Dict[str, Any]:
        """Get file system by name"""

        def _get():
            file_system_id = self._get_file_system_id(file_system_name)
            res = self._get(params={"id": file_system_id})
            if res:
                return res[0]
            else:
                return None

        return self._run_with_spinner("Fetching file system...", _get)

    def create_file_system(
        self,
        name: str,
        size: int,
        region: str,
        gpu_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new file system"""
        request_data = {
            "name": name,
            "size": size,
            "region": region.lower(),
        }

        if gpu_kind:
            request_data["gpu_kind"] = gpu_kind

        def _create():
            return self._post(json=request_data)

        return self._run_with_spinner("Creating file system...", _create)

    def delete_file_system(
        self,
        file_system_name: str,
    ) -> Dict[str, Any]:
        """Delete a file system"""

        def _delete():
            file_system_id = self._get_file_system_id(file_system_name)
            return self._delete(json={"id": file_system_id})

        return self._run_with_spinner("Deleting file system...", _delete)

    def extend_file_system(
        self,
        file_system_name: str,
        size: int,
    ) -> None:
        """Extend a file system"""

        def _extend():
            file_system_id = self._get_file_system_id(file_system_name)
            return self._put(json={"id": file_system_id, "size": size})

        return self._run_with_spinner("Extending file system...", _extend)

    def duplicate_file_system(
        self,
        file_system_name: str,
        duplicate_name: str,
    ) -> None:
        """Duplicate a file system"""

        def _duplicate():
            file_system_id = self._get_file_system_id(file_system_name)
            return self._post(
                path="duplicate",
                json={"id": file_system_id, "duplicate_name": duplicate_name},
            )

        return self._run_with_spinner("Duplicating file system...", _duplicate)


file_systems_api = FileSystemAPI()
