import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import redis.asyncio as redis
from loguru import logger

from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.services.depot_token_cache import DepotTokenCache
from shared.models.depot import DepotBuildCredentials, DepotProject, DepotProjectToken

# Token lifetime when creating new tokens (1 hour)
TOKEN_LIFETIME_HOURS = 1

# Depot API base URL (Connect protocol)
DEPOT_API_BASE = "https://api.depot.dev"


class DepotService:
    """Service for managing Depot projects and tokens for remote builds."""

    def __init__(
        self,
        api_token: str,
        org_id: str,
        enabled: bool = True,
    ):
        self.api_token = api_token
        self.org_id = org_id
        self.enabled = enabled
        self._token_cache = DepotTokenCache()
        self._http_client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        """Check if Depot is properly configured."""
        return self.enabled and bool(self.api_token) and bool(self.org_id)

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client for Depot Connect API."""
        if self._http_client is None:
            logger.info(f"Creating HTTP client for Depot API: {DEPOT_API_BASE}")
            logger.info(f"Using token (first 10 chars): {self.api_token[:10]}...")
            self._http_client = httpx.AsyncClient(
                base_url=DEPOT_API_BASE,
                headers={
                    "Authorization": f"Bearer {self.api_token}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )

        return self._http_client

    async def close(self) -> None:
        """Close connections and cleanup resources."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        if self._token_cache:
            await self._token_cache.close()

    @staticmethod
    def _make_project_name(deployment_id: str) -> str:
        """Generate a Depot project name for a deployment."""
        return f"lazycloud-{deployment_id}"

    @staticmethod
    def _extract_project_id(project_data: dict) -> str | None:
        """Extract project ID from API response data."""
        return project_data.get("projectId") or project_data.get("id")

    @staticmethod
    def _extract_deployment_id(
        deployment: ComposeDeploymentPydantic,
    ) -> str:
        """Extract deployment ID from deployment object."""
        if not deployment.id:
            raise ValueError("Deployment ID is required")

        return str(deployment.id)

    async def _call_api(
        self,
        endpoint: str,
        payload: dict,
        method: str = "POST",
    ) -> dict:
        """Make an API call to Depot Connect RPC endpoint."""
        client = await self._get_http_client()
        full_url = f"{client.base_url}{endpoint}"

        logger.info(
            f"Calling Depot API: {method} {endpoint} with payload: {json.dumps(payload, indent=2)}"
        )

        try:
            if method == "POST":
                response = await client.post(
                    endpoint,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Connect-Protocol-Version": "1",
                    },
                )

            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            logger.info(
                f"API response status: {response.status_code}, "
                f"headers: {dict(response.headers)}"
            )

            response.raise_for_status()

            data = response.json()
            logger.info(
                f"Depot API Response - {method} {endpoint}:\n{json.dumps(data, indent=2)}"
            )

            return data

        except httpx.HTTPStatusError as e:
            error_text = (
                e.response.text[:500] if e.response.text else "No error message"
            )
            logger.error(
                f"Depot API error: {method} {full_url} returned HTTP {e.response.status_code}, "
                f"response: {error_text}"
            )

            raise RuntimeError(
                f"Depot API error: HTTP {e.response.status_code} - {error_text}"
            ) from e

        except Exception as e:
            logger.error(
                f"Unexpected error calling Depot API {endpoint}: {str(e)}",
                exc_info=True,
            )
            raise

    async def create_project(
        self,
        deployment_id: str,
        name: str | None = None,
    ) -> DepotProject:
        """Create a Depot project for a deployment using API."""
        if not self.is_configured:
            raise ValueError("Depot is not configured")

        project_name = name or self._make_project_name(deployment_id)

        logger.info(
            f"Creating Depot project '{project_name}' for deployment {deployment_id}, "
            f"organization {self.org_id}"
        )

        try:
            endpoint = "/depot.core.v1.ProjectService/CreateProject"
            payload = {
                "name": project_name,
                "organizationId": self.org_id,
                "regionId": "us-east-1",
                "cachePolicy": {
                    "keepBytes": 10 * 1024 * 1024 * 1024,  # 10GB
                    "keepDays": 7,
                },
            }

            data = await self._call_api(endpoint, payload)

            # Parse response - Connect RPC wraps responses
            project_data = data.get("project") or data
            project_id = self._extract_project_id(project_data)
            if not project_id:
                logger.error(f"Project ID not found in API response: {data}")
                raise ValueError(f"Project ID not found in API response: {data}")

            project = DepotProject(id=project_id, name=project_name)

            logger.info(
                f"Successfully created Depot project {project.id} ({project.name}) "
                f"for deployment {deployment_id}"
            )
            return project

        except Exception as e:
            logger.error(
                f"Failed to create Depot project '{project_name}' for deployment {deployment_id}: {str(e)}",
                exc_info=True,
            )
            raise RuntimeError(f"Failed to create Depot project: {e}") from e

    async def get_or_create_project(self, deployment_id: str) -> DepotProject:
        """Get existing project or create a new one for the deployment."""
        if not self.is_configured:
            raise ValueError("Depot is not configured")

        # Try to find existing project by name
        project_name = self._make_project_name(deployment_id)
        project = await self._find_project_by_name(project_name)

        if project:
            logger.info(
                f"Found existing Depot project {project.id} for deployment {deployment_id}"
            )
            return project

        # Create new project
        return await self.create_project(deployment_id)

    async def _find_project_by_name(self, name: str) -> DepotProject | None:
        """Find a project by name using Connect HTTP API."""
        logger.info(f"Searching for project '{name}' in organization {self.org_id}")

        try:
            endpoint = "/depot.core.v1.ProjectService/ListProjects"
            payload = {"organizationId": self.org_id}

            data = await self._call_api(endpoint, payload)

            projects = data.get("projects", [])

            for project_data in projects:
                project_name = project_data.get("name")
                project_id = self._extract_project_id(project_data)

                if project_name == name:
                    logger.info(
                        f"Found matching project '{name}' with ID '{project_id}'"
                    )
                    return DepotProject(id=project_id, name=project_name)

            logger.info(f"No project found with name '{name}'")
            return None

        except Exception as e:
            logger.warning(
                f"Failed to find project '{name}': {str(e)}",
                exc_info=True,
            )
            return None

    async def create_project_token(
        self,
        project_id: str,
        description: str | None = None,
    ) -> DepotProjectToken:
        """Create a project token using Depot Connect RPC API."""
        if not self.is_configured:
            raise ValueError("Depot is not configured")

        desc = description or "LazyCloud CLI build token"
        logger.info(
            f"Creating project token for project {project_id} with description: '{desc}'"
        )

        try:
            endpoint = "/depot.core.v1.ProjectService/CreateToken"
            payload = {
                "projectId": project_id,
                "description": desc,
            }

            data = await self._call_api(endpoint, payload)

            # Extract token from "secret" field (Depot API structure: {"tokenId": "...", "secret": "..."})
            if not isinstance(data, dict):
                logger.error(f"Invalid response structure: {data}")
                raise ValueError(f"Invalid response structure: {data}")

            token_value = data.get("secret")
            if not token_value:
                logger.error(
                    f"Token 'secret' field not found in API response. Full data: {data}"
                )
                raise ValueError(
                    f"Token 'secret' field not found in API response: {data}"
                )

            # API doesn't return expiration, use default 1 hour lifetime
            expires_at = datetime.now(timezone.utc) + timedelta(
                hours=TOKEN_LIFETIME_HOURS
            )

            token = DepotProjectToken(token=token_value, expires_at=expires_at)

            logger.info(
                f"Successfully created project token for project {project_id}, "
                f"expires at {expires_at}"
            )
            return token

        except Exception as e:
            logger.error(
                f"Failed to create project token for project {project_id}: {str(e)}",
                exc_info=True,
            )
            raise RuntimeError(f"Failed to create project token: {e}") from e

    async def delete_project(self, project_id: str) -> bool:
        """Delete a Depot project using API."""
        if not self.is_configured:
            raise ValueError("Depot is not configured")

        logger.info(f"Deleting Depot project {project_id}")

        try:
            endpoint = "/depot.core.v1.ProjectService/DeleteProject"
            payload = {"projectId": project_id}

            await self._call_api(endpoint, payload)

            logger.info(f"Successfully deleted Depot project {project_id}")
            return True

        except Exception as e:
            logger.warning(
                f"Failed to delete project {project_id}: {str(e)}",
                exc_info=True,
            )
            return False

    async def delete_deployment_project(
        self,
        deployment: ComposeDeploymentPydantic | None = None,
        deployment_id: str | None = None,
    ) -> bool:
        """Delete a Depot project for a deployment.

        Either deployment object or deployment_id must be provided.
        If deployment object is provided, avoids a database query.
        """
        if not self.is_configured:
            logger.warning("Depot not configured, skipping project deletion")
            return False

        try:
            # Use provided deployment or fetch by ID
            if deployment is None:
                if not deployment_id:
                    raise ValueError(
                        "Either deployment or deployment_id must be provided"
                    )

                deployment = await db.compose_deployments.get_by_id(deployment_id)
                if not deployment:
                    logger.warning(
                        f"Deployment {deployment_id} not found, cannot delete Depot project"
                    )
                    return False

            elif deployment_id is None:
                deployment_id = self._extract_deployment_id(deployment)

            project_name = self._make_project_name(deployment_id)
            project = await self._find_project_by_name(project_name)

            if not project:
                logger.info(f"No Depot project found for deployment {deployment_id}")
                # Still clean up tokens in case they exist
                await self._token_cache.delete_deployment_tokens(deployment_id)
                return True

            logger.info(
                f"Found Depot project {project.id} for deployment {deployment_id}, deleting..."
            )

            success = await self.delete_project(project.id)

            if success:
                logger.info(
                    f"Successfully deleted Depot project {project.id} for deployment {deployment_id}"
                )

            # Always clean up tokens (even if project deletion failed)
            await self._token_cache.delete_deployment_tokens(deployment_id)
            logger.info(f"Cleaned up Depot tokens for deployment {deployment_id}")

            return success

        except Exception as e:
            logger.warning(
                f"Failed to delete Depot project for deployment {deployment_id}: {str(e)}",
                exc_info=True,
            )
            return False

    async def get_build_token(
        self,
        deployment: ComposeDeploymentPydantic,
        registry_url: str,
    ) -> DepotBuildCredentials:
        """Get a build token for a deployment.

        This is the main method called by the API endpoint.
        Checks for a cached valid token first, otherwise creates a new one.
        """
        if not self.is_configured:
            raise ValueError(
                "Depot is not configured. Set DEPOT_ENABLED=true and provide DEPOT_API_TOKEN and DEPOT_ORG_ID."
            )

        deployment_id = self._extract_deployment_id(deployment)

        # Get or create project first (needed to check cached tokens by project_id)
        project = await self.get_or_create_project(deployment_id)

        # Check for cached valid token in Redis (by deployment_id)
        # Redis TTL automatically handles expiration
        cached_token = None
        try:
            cached_token = await self._token_cache.get_token(deployment_id)
        except (redis.RedisError, json.JSONDecodeError, ValueError) as e:
            logger.warning(
                f"Failed to get cached token for deployment {deployment_id}: {e}"
            )

        if cached_token:
            logger.info(
                f"Using cached token for deployment {deployment_id}, "
                f"expires at {cached_token.expires_at}"
            )
            return DepotBuildCredentials(
                project_id=project.id,
                token=cached_token.token,
                expires_at=cached_token.expires_at,
                registry_url=registry_url,
            )

        # No cached token or cache hit failed - create a fresh token with locking
        lock = await self._token_cache.get_lock(deployment_id, timeout=10.0)
        try:
            # Try to acquire lock (non-blocking first)
            acquired = await lock.acquire(blocking=False)

            if not acquired:
                # Another process is creating the token, wait briefly and check cache
                logger.info(
                    f"Lock held by another process for deployment {deployment_id}, "
                    "waiting for token creation..."
                )
                await asyncio.sleep(0.5)
                cached_token = await self._token_cache.get_token(deployment_id)
                if cached_token:
                    logger.info(
                        f"Found token after waiting for deployment {deployment_id}"
                    )
                    return DepotBuildCredentials(
                        project_id=project.id,
                        token=cached_token.token,
                        expires_at=cached_token.expires_at,
                        registry_url=registry_url,
                    )
                # Try blocking acquire with short timeout
                try:
                    acquired = await lock.acquire(blocking=True, blocking_timeout=2.0)
                    if not acquired:
                        raise RuntimeError(
                            f"Could not acquire lock for deployment {deployment_id}"
                        )
                except redis.RedisError as e:
                    raise RuntimeError(
                        f"Failed to acquire lock for deployment {deployment_id}: {e}"
                    ) from e

            # Double-check cache after acquiring lock (another process may have created it)
            cached_token = await self._token_cache.get_token(deployment_id)
            if cached_token:
                logger.info(
                    f"Found cached token after lock acquisition for deployment {deployment_id}"
                )
                return DepotBuildCredentials(
                    project_id=project.id,
                    token=cached_token.token,
                    expires_at=cached_token.expires_at,
                    registry_url=registry_url,
                )

            # Create new token (we hold the lock)
            project_token = await self.create_project_token(
                project_id=project.id,
                description=f"Build token for deployment {deployment_id}",
            )
        except Exception as e:
            logger.error(
                f"Failed to create project token for deployment {deployment_id}: {e}",
                exc_info=True,
            )
            raise RuntimeError(f"Failed to create Depot project token: {e}") from e
        finally:
            # Always release lock if we acquired it
            if lock.owned():
                await lock.release()

        logger.info(f"Created new token for deployment {deployment_id}")

        # Store the new token in Redis with TTL
        try:
            await self._token_cache.set_token(
                deployment_id=deployment_id,
                token=project_token.token,
                expires_at=project_token.expires_at,
            )
            logger.info(f"Cached new token for deployment {deployment_id}")

        except Exception as e:
            # Don't fail the request if caching fails
            logger.warning("Failed to cache token: {}", str(e))

        return DepotBuildCredentials(
            project_id=project.id,
            token=project_token.token,
            expires_at=project_token.expires_at,
            registry_url=registry_url,
        )

    async def get_project_usage(
        self,
        project_id: str,
        start_at: datetime,
        end_at: datetime,
    ) -> dict:
        """Get usage data for a Depot project within a time range."""
        if not self.is_configured:
            raise ValueError("Depot is not configured")

        endpoint = "/depot.core.v1.UsageService/GetProjectUsage"
        # Depot API expects RFC3339 timestamp strings for protobuf Timestamp fields
        payload = {
            "projectId": project_id,
            "startAt": start_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endAt": end_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        try:
            data = await self._call_api(endpoint, payload)
            return data.get("usage", {})

        except Exception as e:
            logger.warning(
                f"Failed to get usage for project {project_id}: {e}",
                exc_info=True,
            )
            return {}

    async def get_deployment_build_minutes(
        self,
        deployment_id: str,
        start_at: datetime,
        end_at: datetime,
    ) -> float:
        """Get build minutes for a specific deployment."""
        if not self.is_configured:
            return 0.0

        try:
            project_name = self._make_project_name(deployment_id)
            project = await self._find_project_by_name(project_name)
            if not project:
                return 0.0

            usage = await self.get_project_usage(project.id, start_at, end_at)

            # Depot returns usage in seconds, convert to minutes
            build_seconds = usage.get("buildDurationSeconds", 0)
            if isinstance(build_seconds, str):
                build_seconds = int(build_seconds)

            build_minutes = build_seconds / 60.0

            logger.debug(
                f"Deployment {deployment_id} build minutes from {start_at} to {end_at}: "
                f"{build_minutes:.2f}"
            )
            return build_minutes

        except Exception as e:
            logger.warning(
                f"Failed to get build minutes for deployment {deployment_id}: {e}",
                exc_info=True,
            )
            return 0.0

    async def get_workspace_build_minutes(
        self,
        workspace_id: str,
        start_at: datetime,
        end_at: datetime,
        deployments: list[ComposeDeploymentPydantic] | None = None,
    ) -> float:
        """Get total build minutes for all deployments in a workspace."""
        if not self.is_configured:
            return 0.0

        total_minutes = 0.0

        try:
            # Use pre-fetched deployments if provided, otherwise query
            if deployments is None:
                deployments = (
                    await db.compose_deployments.find_active_during_date_range(
                        workspace_id=workspace_id,
                        start_date=start_at,
                        end_date=end_at,
                    )
                )

            for deployment in deployments:
                deployment_id = self._extract_deployment_id(deployment)
                build_minutes = await self.get_deployment_build_minutes(
                    deployment_id=deployment_id,
                    start_at=start_at,
                    end_at=end_at,
                )
                total_minutes += build_minutes

            logger.info(
                f"Workspace {workspace_id} build minutes from {start_at} to {end_at}: "
                f"{total_minutes:.2f}"
            )

            return total_minutes

        except Exception as e:
            logger.error(
                f"Failed to get workspace build minutes for {workspace_id}: {e}",
                exc_info=True,
            )
            return 0.0
