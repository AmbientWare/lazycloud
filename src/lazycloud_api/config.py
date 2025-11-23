import json
import os
from enum import StrEnum
from typing import List

import dotenv
from loguru import logger
from pydantic import BaseModel, ConfigDict, model_validator

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

    # AWS Configuration
    AWS_ACCOUNT_ID: str = os.getenv("AWS_ACCOUNT_ID", "")
    AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION: str = os.getenv("AWS_REGION", "")
    AWS_ROUTE53_ZONES: dict[str, str] = json.loads(os.getenv("AWS_ROUTE53_ZONES", "{}"))
    AWS_ENDPOINT_URL: str = os.getenv("AWS_ENDPOINT_URL", None)
    AWS_ECR_BASE_ROLE_ARN: str = os.getenv("AWS_ECR_BASE_ROLE_ARN", "")

    # Cloudflare Configuration
    CLOUDFLARE_API_KEY: str = os.getenv("CLOUDFLARE_API_KEY", "")
    CLOUDFLARE_ZONE_ID: str = os.getenv("CLOUDFLARE_ZONE_ID", "")
    CLOUDFLARE_ACCOUNT_ID: str = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")

    # Database Configurations
    DB_SECRET_KEY: str = os.getenv("DB_SECRET_KEY", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    DATABASE_POOL_URL: str = os.getenv("DATABASE_POOL_URL", "")
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

    # JWT Configuration
    JWT_SECRET: str = os.getenv("JWT_SECRET", "secret-key")
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")

    # Registry Configuration
    REGISTRY_TYPE: str = os.getenv("REGISTRY_TYPE", "docker_hub")
    REGISTRY_URL: str | None = os.getenv("REGISTRY_URL", None)
    REGISTRY_USERNAME: str | None = os.getenv("REGISTRY_USERNAME", None)
    REGISTRY_PASSWORD: str | None = os.getenv("REGISTRY_PASSWORD", None)
    REGISTRY_REGION: str | None = os.getenv("REGISTRY_REGION", None)

    # Monitoring Configuration
    PROMETHEUS_URL: str = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

    # Usage Tracking Configuration
    USAGE_COLLECTION_INTERVAL_HOURS: int = int(
        os.getenv("USAGE_COLLECTION_INTERVAL_HOURS", "1")
    )

    # Billing Configuration
    POLAR_ORGANIZATION_ID: str = os.getenv("POLAR_ORGANIZATION_ID", "")
    POLAR_ACCESS_TOKEN: str = os.getenv("POLAR_ACCESS_TOKEN", "")
    IS_POLAR_SANDBOX: bool = os.getenv("IS_POLAR_SANDBOX", "false").lower() == "true"

    # Secrets Configuration
    SECRETS_TIMEOUT_SECONDS: int = int(os.getenv("SECRETS_TIMEOUT_SECONDS", "30"))

    # Rollback Configuration
    ROLLBACK_JOB_DELETION_TIMEOUT_SECONDS: int = int(
        os.getenv("ROLLBACK_JOB_DELETION_TIMEOUT_SECONDS", "60")
    )
    ROLLBACK_RECONCILIATION_INTERVAL_MINUTES: int = int(
        os.getenv("ROLLBACK_RECONCILIATION_INTERVAL_MINUTES", "10")
    )
    COMPOSE_YAML_MAX_SIZE_BYTES: int = int(
        os.getenv("COMPOSE_YAML_MAX_SIZE_BYTES", "245760")  # 240KB
    )
    HELM_HISTORY_MAX_REVISIONS: int = int(os.getenv("HELM_HISTORY_MAX_REVISIONS", "6"))

    # Invitation Configuration
    INVITATION_EXPIRATION_DAYS: int = int(os.getenv("INVITATION_EXPIRATION_DAYS", "14"))

    # Email Configuration
    RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
    SUPPORT_EMAIL: str = os.getenv("SUPPORT_EMAIL", "support@lazycloud.dev")

    # Required Environment Variables
    required_env_vars: List[str] = [
        "ADMIN_API_KEY",
        "REDIS_URL",
        "AWS_ACCOUNT_ID",
        "AWS_ACCESS_KEY_ID",
        "AWS_ECR_BASE_ROLE_ARN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "AWS_ROUTE53_ZONES",
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

        if not self.PROMETHEUS_URL:
            raise ValueError("PROMETHEUS_URL is required")

        return self


app_config = AppConfig(ENV=ENVIRONMENT(ENV))
