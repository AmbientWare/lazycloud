from lazycloud_cli.api.base import BaseAPI
from shared.responses.users import CurrentUserResponse


class UsersAPI(BaseAPI):
    def __init__(self):
        super().__init__("users")

    def get_user_id(self) -> str | None:
        """Get the user ID"""
        try:
            response: CurrentUserResponse = self._get(path="/current")
            return response.id

        except Exception as e:
            raise e
