from typing import Dict, List, Optional, Tuple, Any
import httpx
from pathlib import Path
from cli.logging import logger
from cli.api.base import BaseAPI
from cli.api.utils import Spinner, StatusSpinner
from cli.config import config


class MachineAPI(BaseAPI):
    def __init__(self):
        super().__init__("machines")

    def _gb_to_mb(self, gb: float) -> int:
        """Convert GB to MB"""
        return int(gb * 1024)

    def list_machines(self) -> List[Dict]:
        """List all machines"""
        try:
            return self._get()

        except Exception as e:
            logger.error(f"Error listing machines: {e}")
            return []

    def get_machines(self, machine_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get machine(s). If machine_name is provided, get that specific machine."""

        def _get():
            if machine_name:
                return self._get(params={"machine_name": machine_name})
            return self._get()

        return self._run_with_spinner("Fetching machines...", _get)

    def get_machine(self, machine_name: str) -> Optional[Dict]:
        """Get a specific machine by name"""
        try:
            return self._get(machine_name)

        except Exception as e:
            logger.error(f"Error getting machine {machine_name}: {e}")
            return None

    def create_machine(
        self,
        name: str,
        public_key: str,
        region: Optional[str] = None,
        cpu_kind: Optional[str] = None,
        cpu: Optional[int] = None,
        memory: Optional[int] = None,
        volume_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a new machine and poll for status updates"""
        request_data = {
            "name": name,
            "public_key": public_key,
        }

        # Add optional fields only if they are explicitly provided
        if region is not None:
            request_data["region"] = region.upper()
        if cpu_kind is not None:
            request_data["cpu_kind"] = cpu_kind
        if cpu is not None:
            request_data["cpu"] = str(cpu)
        if memory is not None:
            request_data["memory"] = str(self._gb_to_mb(memory))
        if volume_size is not None:
            request_data["volume_size"] = str(volume_size)

        def _create():
            return self._post(json=request_data)

        def status_checker():
            machines = self.get_machines(name)
            return str(machines[0].get("status", "Pending")) if machines else "Pending"

        # Create the machine with status polling
        return self._run_with_spinner(
            "Creating machine...", _create, status_checker=status_checker
        )

    def scale_machine(
        self,
        machine_name: str,
        cpu_kind: Optional[str] = None,
        cpu: Optional[int] = None,
        memory: Optional[int] = None,
        region: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Scale a machine"""
        request_data = {}
        if cpu_kind is not None:
            request_data["cpu_kind"] = cpu_kind
        if cpu is not None:
            request_data["cpu"] = str(cpu)
        if memory is not None:
            request_data["memory"] = str(self._gb_to_mb(memory))
        if region is not None:
            request_data["region"] = region.upper()

        def _scale():
            return self._put(machine_name, json=request_data)

        return self._run_with_spinner("Scaling machine...", _scale)

    def extend_volume(self, machine_name: str, volume_size: int) -> None:
        """Extend the volume of a machine"""

        def _extend():
            return self._post(
                f"{machine_name}/volumes", params={"volume_size": volume_size}
            )

        return self._run_with_spinner("Extending volume...", _extend)

    def delete_machine(self, machine_name: str) -> bool:
        """Delete a machine"""
        try:

            def _destroy():
                return self._delete(params={"machine_name": machine_name})

            self._run_with_spinner(f"Destroying machine {machine_name}...", _destroy)
            return True

        except Exception as e:
            logger.error(f"Error deleting machine {machine_name}: {e}")
            return False

    def get_machine_alias(self, machine_name: str) -> Tuple[str | None, int | None]:
        """Get the alias for a machine"""
        res = self._get(f"alias/{machine_name}")
        return res.get("alias"), res.get("port")


machines_api = MachineAPI()
