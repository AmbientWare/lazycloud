"""Token refresh logic for automatic access token renewal."""

import httpx

from cli.config import config


def refresh_access_token(client_id: str, refresh_token: str) -> dict | None:
    """Refresh the access token using a refresh token.

    Returns the new token data or None if refresh failed.
    """
    try:
        response = httpx.post(
            "https://api.workos.com/user_management/authenticate",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            },
            timeout=30.0,
        )

        if response.status_code == 200:
            return response.json()

        return None
    except Exception:
        return None


def get_workos_client_id() -> str | None:
    """Fetch the WorkOS client ID from the backend."""
    try:
        response = httpx.get(
            f"{config.api_base_url}/auth/config",
            timeout=10.0,
        )
        if response.status_code == 200:
            return response.json().get("workos_client_id")
        return None
    except Exception:
        return None


def attempt_token_refresh() -> bool:
    """Attempt to refresh the access token using the stored refresh token.

    Returns True if refresh was successful, False otherwise.
    """
    if config.access_token and config.access_token.startswith("sk_"):
        # API key check - no refresh needed
        return False

    refresh_token = config.refresh_token
    if not refresh_token:
        return False

    try:
        client_id = get_workos_client_id()
        if not client_id:
            return False

        token_data = refresh_access_token(client_id, refresh_token)
        if not token_data:
            return False

        new_access_token = token_data.get("access_token")
        new_refresh_token = token_data.get("refresh_token")
        if new_access_token:
            config.set_tokens(new_access_token, new_refresh_token or refresh_token)
            return True

        return False
    except Exception:
        return False
