import json
import os
from enum import StrEnum
from typing import List

import dotenv
from loguru import logger
from pydantic import BaseModel, ConfigDict, model_validator

from lazycloud_api.registry.base import BaseRegistryConfig
from lazycloud_api.registry.factory import create_registry_config

# we load the environment variables from the .env file first so we can use them in rest of the app
dotenv.load_dotenv()

ENV = os.getenv("ENV", "dev")


class ENVIRONMENT(StrEnum):
    DEV = "dev"
    PROD = "prod"


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)  # Make the config immutable
    ENV: ENVIRONMENT = ENVIRONMENT(ENV)
    ADMIN_API_KEY: str = os.getenv("ADMIN_API_KEY", "")
    RATE_LIMIT: str = os.getenv("RATE_LIMIT", "50/minute")
    IS_WORKER: bool = os.getenv("IS_WORKER", "false").lower() == "true"

    # Logging Configuration
    LOG_LEVEL: str = "DEBUG" if ENV == ENVIRONMENT.DEV else "INFO"

    # API Configuration
    PROJECT_NAME: str = "LazyCloud API"
    PROJECT_VERSION: str = "1.0.0"
    API_VERSION: str = os.getenv("API_VERSION", "/v1")

    # Pricing Configuration
    LAZYCLOUD_UPCHARGE: float = float(os.getenv("LAZYCLOUD_UPCHARGE", 0))
    VOLUME_PRICE: float = float(os.getenv("VOLUME_PRICE", 0))
    DEDICATED_IPV4_PRICE: float = float(os.getenv("DEDICATED_IPV4_PRICE", 0))
    DATA_EGRESS_PRICE: float = float(os.getenv("DATA_EGRESS_PRICE", 0))

    # Fly.io Configuration
    FLY_API_TOKEN: str = os.getenv("FLY_API_TOKEN", "")
    FLY_ORG_NAME: str = os.getenv("FLY_ORG_NAME", "")

    # AWS Configuration
    AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION: str = os.getenv("AWS_REGION", "")
    AWS_ROUTE53_ZONES: dict[str, str] = json.loads(os.getenv("AWS_ROUTE53_ZONES", "{}"))

    # Stripe Configuration
    STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY: str = os.getenv("STRIPE_PUBLISHABLE_KEY", "")

    # Database Configurations
    DB_SECRET_KEY: str = os.getenv("DB_SECRET_KEY", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    DATABASE_POOL_URL: str = os.getenv("DATABASE_POOL_URL", "")
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

    # Registry Configuration
    REGISTRY_TYPE: str = os.getenv("REGISTRY_TYPE", "docker_hub")
    REGISTRY_URL: str | None = os.getenv("REGISTRY_URL", None)
    REGISTRY_USERNAME: str | None = os.getenv("REGISTRY_USERNAME", None)
    REGISTRY_PASSWORD: str | None = os.getenv("REGISTRY_PASSWORD", None)
    REGISTRY_REGION: str | None = os.getenv("REGISTRY_REGION", None)

    @property
    def registry(self) -> BaseRegistryConfig:
        """Get registry configuration based on type."""
        kwargs = {}

        if self.REGISTRY_URL:
            kwargs["registry_url"] = self.REGISTRY_URL
        if self.REGISTRY_USERNAME:
            kwargs["username"] = self.REGISTRY_USERNAME
        if self.REGISTRY_PASSWORD:
            kwargs["password"] = self.REGISTRY_PASSWORD
        if self.REGISTRY_REGION:
            kwargs["region"] = self.REGISTRY_REGION

        return create_registry_config(self.REGISTRY_TYPE, **kwargs)

    # Required Environment Variables
    required_env_vars: List[str] = [
        "ADMIN_API_KEY",
        "LAZYCLOUD_UPCHARGE",
        "VOLUME_PRICE",
        "DEDICATED_IPV4_PRICE",
        "DATA_EGRESS_PRICE",
        "FLY_API_TOKEN",
        "FLY_ORG_NAME",
        "REDIS_URL",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "AWS_ROUTE53_ZONES",
        "STRIPE_SECRET_KEY",
        "STRIPE_PUBLISHABLE_KEY",
        "DB_SECRET_KEY",
    ]

    @model_validator(mode="after")
    def validate_required_env_vars(self: "AppConfig") -> "AppConfig":
        logger.info("Validating required environment variables")
        missing_vars = [var for var in self.required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing_vars)}"
            )

        # validate that if IS_WORKER, we have a DATABASE_POOL_URL otherwise we have a DATABASE_URL
        if self.IS_WORKER and not self.DATABASE_POOL_URL:
            raise ValueError("DATABASE_POOL_URL is required when IS_WORKER is true")

        elif not self.IS_WORKER and not self.DATABASE_URL:
            raise ValueError("DATABASE_URL is required when IS_WORKER is false")

        return self


app_config = AppConfig(ENV=ENVIRONMENT(ENV))
