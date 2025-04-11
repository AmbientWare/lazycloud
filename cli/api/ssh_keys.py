from typing import Dict, List, Optional, Any
from pathlib import Path
from cli.logging import logger
from cli.api.base import BaseAPI
from cli.api.utils import Spinner


class SSHKeysAPI(BaseAPI):
    def __init__(self):
        super().__init__("ssh-keys")

    def get_ssh_keys(self) -> List[Dict[str, Any]]:
        """Get all SSH keys"""
        try:
            def _get():
                return self._get()

            return self._run_with_spinner("Fetching SSH keys...", _get)
        except Exception as e:
            logger.error(f"Error getting SSH keys: {e}")
            return []

    def create_ssh_key(self, name: str, public_key: str) -> Optional[Dict[str, Any]]:
        """Create a new SSH key"""
        try:
            def _create():
                return self._post(json={"name": name, "public_key": public_key})

            return self._run_with_spinner("Creating SSH key...", _create)
        except Exception as e:
            logger.error(f"Error creating SSH key: {e}")
            return None

    def delete_ssh_key(self, ssh_key_id: int) -> bool:
        """Delete an SSH key"""
        try:
            def _delete():
                return self._delete(json={"ssh_key_id": ssh_key_id})

            self._run_with_spinner("Deleting SSH key...", _delete)
            return True
        except Exception as e:
            logger.error(f"Error deleting SSH key: {e}")
            return False

    @staticmethod
    def read_public_key(key_path: str) -> str:
        """Read and validate a public key file"""
        path = Path(str(key_path))
        if not path.exists():
            raise FileNotFoundError(f"Public key file not found at {key_path}")

        with open(path) as f:
            return f.read().strip()


ssh_keys_api = SSHKeysAPI()
