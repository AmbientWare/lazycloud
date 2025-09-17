from typing import Any, Dict, Optional

from lazycloud_cli.api.base import BaseAPI
from lazycloud_cli.api.response_models import UserInfoResponse
from lazycloud_cli.logging import logger


class UsersAPI(BaseAPI):
    def __init__(self):
        super().__init__("users")

    def get_user_id(self) -> str | None:
        """Get the user ID"""
        try:
            response = self._get("id")
            if isinstance(response, dict) and "user_id" in response:
                return response["user_id"]
            # Handle legacy response format
            return response if isinstance(response, str) else None

        except Exception:
            return None

    def get_user_info(self) -> Optional[UserInfoResponse]:
        """Get user information"""
        try:
            response = self._get()
            return UserInfoResponse(**response)

        except Exception as e:
            logger.error(f"Error getting user info: {e}")
            return None

    def update_user_info(self, user_data: Dict[str, Any]) -> Optional[UserInfoResponse]:
        """Update user information"""
        try:

            def _update():
                return self._put(json=user_data)

            response = self._run_with_spinner("Updating user info...", _update)
            return UserInfoResponse(**response) if response else None

        except Exception as e:
            logger.error(f"Error updating user info: {e}")
            return None


users_api = UsersAPI()
