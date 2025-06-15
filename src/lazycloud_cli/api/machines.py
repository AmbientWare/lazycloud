from typing import Dict, List, Optional, Tuple, Any
import typer

from lazycloud_cli.logging import logger
from lazycloud_cli.api.base import BaseAPI
from lazycloud_cli.api.utils import mb_to_gb
from lazycloud_cli.api.tasks import tasks_api, TaskStatus


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
        disk_size: int,
        region: Optional[str] = None,
        cpu: Optional[int] = None,
        memory: Optional[int] = None,
        gpu_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new machine and poll for status updates"""
        request_data = {
            "name": name,
            "public_key": public_key,
            "disk_size": disk_size,
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

        # Store task_id for status checker
        task_id_container = {"task_id": None}

        status_checker = self._create_status_checker(
            task_id_container, "Creating machine...", "Machine creation completed", name
        )

        # Modified create function to capture task_id
        def _create_and_wait_with_task_id():
            # Phase 1: Create machine (get task_id)
            response = self._post(json=request_data)
            task_id = response.get("task_id")
            task_id_container["task_id"] = task_id  # Store for status checker

            if not task_id:
                return response

            # Phase 2: Poll task status until completion using tasks API
            try:
                tasks_api.wait_for_task_completion(task_id)
                return {"status": "success", "message": "Machine creation completed"}

            except Exception as e:
                raise Exception(f"Machine creation failed: {str(e)}")

        # Create the machine with two-phase status polling
        return self._run_with_spinner(
            "Creating machine...",
            _create_and_wait_with_task_id,
            status_checker=status_checker,
        )

    def _create_status_checker(
        self,
        task_id_container: dict,
        in_progress_message: str,
        completed_message: str,
        machine_name: Optional[str] = None,
    ):
        """Create a reusable status checker function for machine operations"""

        def status_checker():
            try:
                # If we don't have task_id yet, we're still starting
                if not task_id_container["task_id"]:
                    return "Pending..."

                task_status_response = tasks_api.get_task_status(
                    task_id_container["task_id"]
                )
                task_status = task_status_response.get("status", "unknown")

                if task_status == TaskStatus.QUEUED:
                    return "Task queued"

                elif task_status == TaskStatus.IN_PROGRESS:
                    # For create operations, provide more detailed status
                    if machine_name and "Creating" in in_progress_message:
                        try:
                            machines = self.get_machines(
                                machine_name, with_spinner=False
                            )
                            if machines:
                                machine_status = machines[0].get("status", "unknown")
                                return f"Creating ({machine_status})"
                            else:
                                return "Creating (initializing)"
                        except Exception as e:
                            logger.error(f"Error getting machine status: {e}")
                            return in_progress_message
                    else:
                        return in_progress_message

                elif task_status == TaskStatus.COMPLETED:
                    # For create operations, check final machine status
                    if machine_name and "Creating" in in_progress_message:
                        try:
                            machines = self.get_machines(
                                machine_name, with_spinner=False
                            )
                            if machines:
                                return str(machines[0].get("status", "Deployed"))
                            return "Deployed"
                        except Exception as e:
                            logger.error(f"Error getting machine status: {e}")
                            return completed_message
                    else:
                        return completed_message

                else:
                    return f"Task {task_status}"

            except Exception as e:
                logger.error(f"Error getting task status: {e}")
                return "Processing..."

        return status_checker

    def _create_task_wrapper(self, task_id_container: dict, operation_name: str):
        """Create a reusable task wrapper that handles task completion polling"""

        def task_wrapper(api_response: Dict[str, Any]):
            task_id = api_response.get("task_id")
            task_id_container["task_id"] = task_id  # Store for status checker

            if not task_id:
                return api_response

            # Poll task status until completion
            try:
                tasks_api.wait_for_task_completion(task_id)
                return {"status": "success", "message": f"{operation_name} completed"}

            except Exception as e:
                raise Exception(f"{operation_name} failed: {str(e)}")

        return task_wrapper

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

        # Store task_id for status checker
        task_id_container = {"task_id": None}

        status_checker = self._create_status_checker(
            task_id_container, "Scaling machine...", "Scaled successfully", machine_name
        )

        task_wrapper = self._create_task_wrapper(task_id_container, "Machine scaling")

        def _scale_and_wait():
            machine_id = self._get_machine_id(machine_name)
            response = self._put(str(machine_id), json=request_data)
            return task_wrapper(response)

        return self._run_with_spinner(
            "Scaling machine...",
            _scale_and_wait,
            status_checker=status_checker,
        )

    def delete_machine(self, machine_name: str) -> Dict[str, Any] | None:
        """Delete a machine"""
        try:
            # Store task_id for status checker
            task_id_container = {"task_id": None}

            status_checker = self._create_status_checker(
                task_id_container,
                "Deleting machine...",
                "Deleted successfully",
                machine_name,
            )

            task_wrapper = self._create_task_wrapper(
                task_id_container, "Machine deletion"
            )

            def _destroy_and_wait():
                machine_id = self._get_machine_id(machine_name)
                response = self._delete(params={"id": machine_id})
                return task_wrapper(response)

            response = self._run_with_spinner(
                f"Destroying machine {machine_name}...",
                _destroy_and_wait,
                status_checker=status_checker,
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
        # Store task_id for status checker
        task_id_container = {"task_id": None}

        status_checker = self._create_status_checker(
            task_id_container,
            "Restarting machine...",
            "Restarted successfully",
            machine_name,
        )

        task_wrapper = self._create_task_wrapper(task_id_container, "Machine restart")

        def _restart_and_wait():
            machine_id = self._get_machine_id(machine_name)
            response = self._post(f"{machine_id}/restart", json={})
            return task_wrapper(response)

        return self._run_with_spinner(
            "Restarting machine...",
            _restart_and_wait,
            status_checker=status_checker,
        )

    def auto_stop(self, machine_name: str, enabled: bool) -> Dict[str, Any]:
        """Auto stop a machine"""
        # Store task_id for status checker
        task_id_container = {"task_id": None}

        status_checker = self._create_status_checker(
            task_id_container,
            "Updating auto-stop settings...",
            "Auto-stop settings updated",
            machine_name,
        )

        task_wrapper = self._create_task_wrapper(task_id_container, "Auto-stop update")

        def _auto_stop_and_wait():
            machine_id = self._get_machine_id(machine_name)
            data = {"enabled": enabled}
            response = self._post(f"{machine_id}/auto-stop", json=data)
            return task_wrapper(response)

        return self._run_with_spinner(
            "Enabling auto stop..."
            if enabled
            else "Making sure machine is kept alive...",
            _auto_stop_and_wait,
            status_checker=status_checker,
        )


machines_api = MachineAPI()
