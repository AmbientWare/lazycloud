from typing import Dict, List, Optional, Tuple, Any
import typer
from pydantic import BaseModel

from lazycloud_cli.logging import logger
from lazycloud_cli.api.base import BaseAPI
from lazycloud_cli.api.utils import mb_to_gb


class GPUInfo(BaseModel):
    regions: List[str]


class MachineOptions(BaseModel):
    regions: List[str]
    compute: Dict[str, List[int]]
    gpu: Dict[str, GPUInfo]


class MachineAPI(BaseAPI):
    def __init__(self):
        super().__init__("machines")

    def _gb_to_mb(self, gb: float) -> int:
        """Convert GB to MB"""
        return int(gb * 1024)

    def _get_machine_id(self, name: str, raise_error: bool = True) -> int | None:
        """Get the machine ID for a machine"""
        machines = self._get(params={"name": name})
        if not machines:
            if raise_error:
                logger.error(f"Machine {name} not found")
                raise typer.Exit(1)
            else:
                return None

        machine_id = machines[0].get("id")
        if not machine_id:
            logger.error(f"Machine {name} has no ID")
            raise typer.Exit(1)

        return machine_id

    def get_machine_options(self) -> MachineOptions:
        """Get the options for a machine"""
        res = self._get("options")
        return MachineOptions(
            regions=res.get("regions", []),
            compute=res.get("compute", {}),
            gpu=res.get("gpu", {}),
        )

    def list_machines(self) -> List[Dict]:
        """List all machines"""
        try:
            res = self.get_machines(with_spinner=False)

            return res

        except Exception as e:
            logger.error(f"Error listing machines: {e}")
            return []

    def get_machines(
        self, machine_name: Optional[str] = None, with_spinner: bool = True
    ) -> List[Dict[str, Any]]:
        """Get machine(s). If machine_name is provided, get that specific machine."""

        def _get():
            if machine_name:
                machine_id = self._get_machine_id(machine_name, raise_error=False)
                if machine_id:
                    res = self._get(params={"id": machine_id})
                else:
                    res = []
            else:
                res = self._get()

            for machine in res:
                if machine.get("memory"):
                    machine["memory"] = mb_to_gb(machine["memory"])

            return res

        return (
            self._run_with_spinner("Fetching machines...", _get)
            if with_spinner
            else _get()
        )

    def create_machine(
        self,
        name: str,
        public_key: str,
        file_system_id: int,
        region: Optional[str] = None,
        cpu: Optional[int] = None,
        memory: Optional[int] = None,
        gpu_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new machine and poll for status updates"""
        request_data = {
            "name": name,
            "public_key": public_key,
            "file_system_id": file_system_id,
        }

        # Add optional fields only if they are explicitly provided
        if region is not None:
            request_data["region"] = region.lower()
        if cpu is not None:
            request_data["cpu"] = str(cpu)
        if memory is not None:
            request_data["memory"] = str(self._gb_to_mb(memory))
        if gpu_kind is not None:
            request_data["gpu_kind"] = gpu_kind

        def _create():
            return self._post(json=request_data)

        def status_checker():
            machines = self.get_machines(name, with_spinner=False)
            return str(machines[0].get("status", "Pending")) if machines else "Pending"

        # Create the machine with status polling
        return self._run_with_spinner(
            "Creating machine...", _create, status_checker=status_checker
        )

    def scale_machine(
        self,
        machine_name: str,
        cpu: Optional[int] = None,
        memory: Optional[int] = None,
        region: Optional[str] = None,
        gpu_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Scale a machine"""
        request_data = {}
        if cpu is not None:
            request_data["cpu"] = str(cpu)
        if memory is not None:
            request_data["memory"] = str(self._gb_to_mb(memory))
        if region is not None:
            request_data["region"] = region.upper()
        if gpu_kind is not None:
            request_data["gpu_kind"] = gpu_kind

        def _scale():
            machine_id = self._get_machine_id(machine_name)
            return self._put(str(machine_id), json=request_data)

        return self._run_with_spinner("Scaling machine...", _scale)

    def delete_machine(self, machine_name: str) -> Dict[str, Any] | None:
        """Delete a machine"""
        try:

            def _destroy():
                machine_id = self._get_machine_id(machine_name)
                return self._delete(params={"id": machine_id})

            response = self._run_with_spinner(
                f"Destroying machine {machine_name}...", _destroy
            )
            return response

        except Exception as e:
            logger.error(f"Error deleting machine {machine_name}: {e}")
            return None

    def connection_details(self, machine_name: str) -> Tuple[str | None, int | None]:
        """Get the IP address and port for a machine"""
        machine_id = self._get_machine_id(machine_name)
        res = self._get(f"{machine_id}/connection-details")
        return res.get("ip"), res.get("port")

    def restart(self, machine_name: str) -> Dict[str, Any]:
        """Restart a machine"""

        def _restart():
            machine_id = self._get_machine_id(machine_name)
            return self._post(f"{machine_id}/restart", json={})

        return self._run_with_spinner("Restarting machine...", _restart)

    def auto_stop(self, machine_name: str, enabled: bool) -> Dict[str, Any]:
        """Auto stop a machine"""

        def _auto_stop():
            machine_id = self._get_machine_id(machine_name)
            data = {"enabled": enabled}
            return self._post(f"{machine_id}/auto-stop", json=data)

        return self._run_with_spinner(
            "Enabling auto stop..." if enabled else "Making sure machine is kept alive...",
            _auto_stop,
        )


machines_api = MachineAPI()
