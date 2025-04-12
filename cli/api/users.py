from typing import Dict, Optional, Any
from cli.logging import logger
from cli.api.base import BaseAPI
from cli.api.utils import Spinner


class UsersAPI(BaseAPI):
    def __init__(self):
        super().__init__("users")

    def get_user_id(self) -> str:
        """Get the user ID"""
        try:
            return self._get("id")

        except Exception as e:
            logger.error(f"Error getting user ID: {e}")
            return ""

    def get_user_info(self) -> Optional[Dict[str, Any]]:
        """Get user information"""
        try:
            return self._get()

        except Exception as e:
            logger.error(f"Error getting user info: {e}")
            return None

    def update_user_info(self, user_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update user information"""
        try:

            def _update():
                return self._put(json=user_data)

            return self._run_with_spinner("Updating user info...", _update)

        except Exception as e:
            logger.error(f"Error updating user info: {e}")
            return None


users_api = UsersAPI()
