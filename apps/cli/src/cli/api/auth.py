import time

import httpx

from cli.api.base import BaseAPI


class AuthAPI(BaseAPI):
    """API for authentication operations (public endpoints, no auth required)."""

    def __init__(self):
        super().__init__("auth", use_version=False)

    def _get_headers(self) -> dict[str, str]:
        """Override to return empty headers - auth endpoints are public."""
        return {}

    def get_config(self) -> dict:
        """Fetch authentication configuration from the backend."""
        return self._get("/config")

    def request_device_authorization(self, client_id: str) -> dict:
        """Request device authorization from WorkOS."""
        response = httpx.post(
            "https://api.workos.com/user_management/authorize/device",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"client_id": client_id},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def poll_for_tokens(
        self,
        client_id: str,
        device_code: str,
        expires_in: int = 300,
        interval: int = 5,
    ) -> dict:
        """Poll WorkOS for tokens after user authenticates."""
        deadline = time.time() + expires_in

        while time.time() < deadline:
            response = httpx.post(
                "https://api.workos.com/user_management/authenticate",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": client_id,
                },
                timeout=self.timeout,
            )

            data = response.json()

            if response.status_code == 200:
                return data

            error = data.get("error")
            if error == "authorization_pending":
                time.sleep(interval)
                continue
            elif error == "slow_down":
                interval += 1
                time.sleep(interval)
                continue
            elif error in ("access_denied", "expired_token"):
                raise Exception(f"Authorization failed: {error}")
            else:
                raise Exception(
                    f"Authorization failed: {data.get('error_description', error)}"
                )

        raise Exception("Authorization timed out")
