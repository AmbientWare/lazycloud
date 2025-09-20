from lazycloud_cli.api.base import BaseAPI


class UsersAPI(BaseAPI):
    def __init__(self):
        super().__init__("users")

    def get_user_id(self) -> str | None:
        """Get the user ID"""
        try:
            response = self._get(path="/id")
            if isinstance(response, dict) and "user_id" in response:
                return response["user_id"]
            # Handle legacy response format
            return response if isinstance(response, str) else None

        except Exception:
            return None
