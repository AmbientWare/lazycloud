import httpx
from typing import Optional, List, Dict, Any, Tuple
from pathlib import Path
from cli.paths.utils import Spinner, StatusSpinner
from cli.config import config


class MachineAPI:
    def __init__(self, timeout: float = 300.0):
        self.timeout = timeout
        self.machine_url = f"{config.api_url}/machines"
        self.user_url = f"{config.api_url}/users"

    def _get_client(self) -> httpx.Client:
        """Get an HTTP client with authentication"""
        headers = {}
        if config.active_api_key:
            api_key = config.active_api_key
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

        else:
            raise Exception(
                "No active api key found. Please run `machines api-key add` to add an api key."
            )

        return httpx.Client(timeout=self.timeout, headers=headers)

    def _run_with_spinner(
        self, message: str, func, status_polling: Optional[Tuple[str, str]] = None
    ):
        """Run a function with a spinner in a separate thread. If status_polling is provided,
        it should be a tuple of (machine_name, status_message) to poll for status updates.
        """
        if status_polling:
            machine_name, status_message = status_polling

            def status_checker():
                machines = self.get_machines(machine_name)
                return machines[0].get("status", "Pending") if machines else "Pending"

            spinner = StatusSpinner(status_message, status_checker)

        else:
            spinner = Spinner(message)

        with spinner:
            return func()

    def _gb_to_mb(self, gb: float) -> int:
        """Convert GB to MB"""
        return int(gb * 1024)

    def _make_request(
        self,
        method: str,
        url: str,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Make a request to the API"""
        with self._get_client() as client:
            try:
                response = client.request(method, url, json=json)
                response.raise_for_status()
                return response.json() if response.content else None

            except httpx.RequestError as e:
                raise Exception(str(e))

            except httpx.HTTPStatusError as e:
                error_data = e.response.json()
                raise Exception(error_data["detail"])

    def get_user_id(self) -> str:
        """Get the user ID"""
        return self._make_request("GET", f"{self.user_url}/id")

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
            return self._make_request("POST", self.machine_url, json=request_data)

        # Create the machine with status polling
        return self._run_with_spinner(
            "Creating machine...", _create, status_polling=(name, "Creating machine")
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
            return self._make_request(
                "PUT", f"{self.machine_url}/{machine_name}", json=request_data
            )

        return self._run_with_spinner("Scaling machine...", _scale)

    def extend_volume(self, machine_name: str, volume_size: int) -> None:
        """Extend the volume of a machine"""

        def _extend():
            return self._make_request(
                "POST",
                f"{self.machine_url}/{machine_name}/volumes?volume_size={volume_size}",
            )

        return self._run_with_spinner("Extending volume...", _extend)

    def destroy_machine(self, machine_name: str) -> None:
        """Destroy a machine"""

        def _destroy():
            return self._make_request(
                "DELETE", f"{self.machine_url}?machine_name={machine_name}"
            )

        self._run_with_spinner(f"Destroying machine {machine_name}...", _destroy)

    def get_machines(self, machine_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get machine(s). If machine_name is provided, get that specific machine."""
        url = self.machine_url
        if machine_name:
            url = f"{url}?machine_name={machine_name}"

        def _get():
            return self._make_request("GET", url)

        return self._run_with_spinner("Fetching machines...", _get)

    def get_machine_alias(self, machine_name: str) -> Tuple[str | None, int | None]:
        """Get the alias for a machine"""
        url = f"{self.machine_url}/alias/{machine_name}"

        res = self._make_request("GET", url)

        return res.get("alias"), res.get("port")

    @staticmethod
    def read_public_key(key_path: str) -> str:
        """Read and validate a public key file"""
        path = Path(str(key_path))
        if not path.exists():
            raise FileNotFoundError(f"Public key file not found at {key_path}")

        with open(path) as f:
            return f.read().strip()


machines_api = MachineAPI()
