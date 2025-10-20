import argparse
import asyncio
from typing import Any

import httpx
from loguru import logger

from lazycloud_api.config import app_config


class CloudflareAPIError(Exception):
    """Custom exception for Cloudflare API errors"""

    def __init__(
        self, message: str, status_code: int | None = None, errors: list | None = None
    ):
        self.status_code = status_code
        self.errors = errors or []
        super().__init__(message)


class CloudflareService:
    """Service for managing Cloudflare SSL for SaaS custom hostnames"""

    def __init__(self):
        self._base_url = "https://api.cloudflare.com/client/v4"
        self._api_key = app_config.CLOUDFLARE_API_KEY
        self._zone_id = app_config.CLOUDFLARE_ZONE_ID
        self._account_id = app_config.CLOUDFLARE_ACCOUNT_ID
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create an async HTTP client"""
        if self._client is None:
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to the Cloudflare API"""
        client = self._get_client()

        try:
            response = await client.request(
                method=method,
                url=endpoint,
                json=json_data,
                params=params,
            )

            response_data = response.json()

            # Cloudflare API returns success: false for errors
            if not response_data.get("success", False):
                errors = response_data.get("errors", [])
                error_messages = [
                    f"{err.get('code', 'unknown')}: {err.get('message', 'Unknown error')}"
                    for err in errors
                ]
                error_msg = (
                    "; ".join(error_messages) if error_messages else "Unknown error"
                )

                logger.error(f"Cloudflare API error: {error_msg}")
                raise CloudflareAPIError(
                    message=error_msg,
                    status_code=response.status_code,
                    errors=errors,
                )

            return response_data.get("result", {})

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error during Cloudflare API request: {e}")
            raise CloudflareAPIError(
                message=f"HTTP {e.response.status_code}: {e.response.reason_phrase}",
                status_code=e.response.status_code,
            ) from e

        except httpx.RequestError as e:
            logger.error(f"Request error during Cloudflare API request: {e}")
            raise CloudflareAPIError(
                message=f"Request failed: {str(e)}",
            ) from e

    async def add_saas_domain(
        self,
        domain: str,
        ssl_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a custom hostname for SSL for SaaS"""
        # Default SSL settings for SaaS
        default_ssl_settings = {
            "method": "http",
            "type": "dv",
            "wildcard": False,
        }

        ssl_config = ssl_settings or default_ssl_settings

        payload = {
            "hostname": domain,
            "ssl": ssl_config,
        }

        logger.info(f"Adding custom hostname: {domain}")
        result = await self._make_request(
            method="POST",
            endpoint=f"/zones/{self._zone_id}/custom_hostnames",
            json_data=payload,
        )

        logger.info(
            f"Successfully created custom hostname {domain} with ID: {result.get('id')}"
        )
        return result

    async def _get_hostname_id_by_domain(self, domain: str) -> str | None:
        """Look up a custom hostname ID by domain name

        Args:
            domain: The domain name to look up

        Returns:
            The hostname ID if found, None otherwise
        """
        client = self._get_client()
        response = await client.get(
            f"/zones/{self._zone_id}/custom_hostnames",
            params={"hostname": domain},
        )

        response_data = response.json()

        if not response_data.get("success", False):
            errors = response_data.get("errors", [])
            error_messages = [
                f"{err.get('code', 'unknown')}: {err.get('message', 'Unknown error')}"
                for err in errors
            ]
            error_msg = "; ".join(error_messages) if error_messages else "Unknown error"

            logger.error(f"Cloudflare API error: {error_msg}")
            raise CloudflareAPIError(
                message=error_msg,
                status_code=response.status_code,
                errors=errors,
            )

        results = response_data.get("result", [])
        if results and len(results) > 0:
            # Find exact match
            for result in results:
                if result.get("hostname") == domain:
                    return result.get("id")

        return None

    async def delete_saas_domain(self, domain: str) -> dict[str, Any]:
        """Delete a custom hostname by domain name"""
        logger.info(f"Looking up hostname ID for domain: {domain}")
        hostname_id = await self._get_hostname_id_by_domain(domain)

        if not hostname_id:
            raise CloudflareAPIError(
                message=f"Custom hostname not found: {domain}",
                status_code=404,
            )

        logger.info(f"Deleting custom hostname: {domain} (ID: {hostname_id})")
        result = await self._make_request(
            method="DELETE",
            endpoint=f"/zones/{self._zone_id}/custom_hostnames/{hostname_id}",
        )

        logger.info(f"Successfully deleted custom hostname: {domain}")
        return result

    async def get_domain_status(self, domain: str) -> dict[str, Any]:
        """Get the status of a custom hostname by domain name"""
        logger.debug(f"Looking up hostname ID for domain: {domain}")
        hostname_id = await self._get_hostname_id_by_domain(domain)

        if not hostname_id:
            raise CloudflareAPIError(
                message=f"Custom hostname not found: {domain}",
                status_code=404,
            )

        logger.debug(
            f"Getting status for custom hostname: {domain} (ID: {hostname_id})"
        )
        result = await self._make_request(
            method="GET",
            endpoint=f"/zones/{self._zone_id}/custom_hostnames/{hostname_id}",
        )

        return result

    async def aclose(self) -> None:
        """Close the HTTP client and clean up resources"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.debug("Cloudflare service client closed")

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", "-d", type=str, required=True)
    parser.add_argument("--action", "-a", type=str, required=True)
    args = parser.parse_args()

    async def main():
        cloudflare_service = CloudflareService()

        # Add a domain
        result = await cloudflare_service.add_saas_domain(domain=args.domain)
        logger.info(f"Added domain result: {result}")

        # Get domain status
        status = await cloudflare_service.get_domain_status(domain=args.domain)
        logger.info(f"Domain status: {status}")

        # Delete domain (if action is delete)
        if args.action == "delete":
            delete_result = await cloudflare_service.delete_saas_domain(
                domain=args.domain
            )
            logger.info(f"Deleted domain result: {delete_result}")

        await cloudflare_service.aclose()

    asyncio.run(main())
