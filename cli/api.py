import httpx
from typing import Optional, List, Dict, Any
from pathlib import Path
import threading
import time
from cli.utils import Spinner
from cli.config import config


class MachineAPI:
    def __init__(self, timeout: float = 300.0):
        self.timeout = timeout
        self.base_url = f"{config.api_url}/machines"

    def _get_client(self) -> httpx.Client:
        """Get an HTTP client with authentication"""
        headers = {}
        if config.active_token:
            token_value = config.get_token()
            if token_value:
                headers["Authorization"] = f"Bearer {token_value}"

        else:
            raise Exception(
                "No active token found. Please run `machines token add` to add a token."
            )

        return httpx.Client(timeout=self.timeout, headers=headers)

    def _run_with_spinner(self, message: str, func):
        """Run a function with a spinner in a separate thread"""
        spinner = Spinner(message)
        spinner_thread = threading.Thread(target=self._spin_thread, args=(spinner,))
        spinner_thread.daemon = True

        with spinner:
            spinner_thread.start()
            try:
                result = func()
                return result
            finally:
                # kill the spinner so it doesn't continue to run
                spinner.running = False

    def _gb_to_mb(self, gb: float) -> int:
        """Convert GB to MB"""
        return int(gb * 1024)

    def _spin_thread(self, spinner: Spinner):
        """Run the spinner in a separate thread"""
        while True:
            spinner.spin()
            time.sleep(0.1)

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
        """Create a new machine"""
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
            return self._make_request("POST", self.base_url, json=request_data)

        return self._run_with_spinner("Creating machine...", _create)

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
                "PUT", f"{self.base_url}/{machine_name}", json=request_data
            )

        return self._run_with_spinner("Scaling machine...", _scale)

    def extend_volume(self, machine_name: str, volume_size: int) -> None:
        """Extend the volume of a machine"""

        def _extend():
            return self._make_request(
                "POST",
                f"{self.base_url}/{machine_name}/volumes?volume_size={volume_size}",
            )

        return self._run_with_spinner("Extending volume...", _extend)

    def destroy_machine(self, machine_name: str) -> None:
        """Destroy a machine"""

        def _destroy():
            return self._make_request("DELETE", f"{self.base_url}/{machine_name}")

        self._run_with_spinner("Destroying machine...", _destroy)

    def get_machines(self, machine_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get machine(s). If machine_name is provided, get that specific machine."""
        url = self.base_url
        if machine_name:
            url = f"{url}?machine_name={machine_name}"

        def _get():
            return self._make_request("GET", url)

        return self._run_with_spinner("Fetching machines...", _get)

    def get_machine_alias(self, machine_name: str) -> str:
        """Get the alias for a machine"""
        url = f"{self.base_url}/alias/{machine_name}"

        return self._make_request("GET", url)

    @staticmethod
    def read_public_key(key_path: str) -> str:
        """Read and validate a public key file"""
        path = Path(str(key_path))
        if not path.exists():
            raise FileNotFoundError(f"Public key file not found at {key_path}")

        with open(path) as f:
            return f.read().strip()
