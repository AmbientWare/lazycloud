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

    # Logging Configuration
    LOG_LEVEL: str = "DEBUG" if ENV == ENVIRONMENT.DEV else "INFO"

    # API Configuration
    PROJECT_NAME: str = "LazyCloud API"
    PROJECT_VERSION: str = "1.0.0"
    API_VERSION: str = os.getenv("API_VERSION", "/v1")
    CORS_ORIGINS: list[str] = json.loads(
        os.getenv("CORS_ORIGINS", '["http://localhost:3000"]')
    )

    # Cloud provider ("aws" or "hetzner")
    CLOUD_PROVIDER: str = os.getenv("CLOUD_PROVIDER", "hetzner")

    # AWS Configuration (optional, only needed when CLOUD_PROVIDER=aws)
    AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION: str = os.getenv("AWS_REGION", "")
    AWS_ENDPOINT_URL: str = os.getenv("AWS_ENDPOINT_URL", None)

    # Cloudflare Configuration
    CLOUDFLARE_API_KEY: str = os.getenv("CLOUDFLARE_API_KEY", "")
    CLOUDFLARE_ZONE_ID: str = os.getenv("CLOUDFLARE_ZONE_ID", "")
    CLOUDFLARE_ACCOUNT_ID: str = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")

    # Database Configurations
    DB_SECRET_KEY: str = os.getenv("DB_SECRET_KEY", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    DATABASE_POOL_URL: str = os.getenv("DATABASE_POOL_URL", "")
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

    # WorkOS Configuration (for token validation via JWKS)
    WORKOS_CLIENT_ID: str = os.getenv("WORKOS_CLIENT_ID", "")

    # Registry Configuration
    REGISTRY_TYPE: str = os.getenv("REGISTRY_TYPE", "docker_hub")
    REGISTRY_URL: str | None = os.getenv("REGISTRY_URL", None)
    REGISTRY_USERNAME: str | None = os.getenv("REGISTRY_USERNAME", None)
    REGISTRY_PASSWORD: str | None = os.getenv("REGISTRY_PASSWORD", None)
    REGISTRY_REGION: str | None = os.getenv("REGISTRY_REGION", None)

    # Depot Configuration (Remote Builds)
    DEPOT_ENABLED: bool = os.getenv("DEPOT_ENABLED", "true").lower() == "true"
    DEPOT_API_TOKEN: str = os.getenv("DEPOT_API_TOKEN", "")
    DEPOT_ORG_ID: str = os.getenv("DEPOT_ORG_ID", "")
    DEPOT_REGISTRY_URL: str = os.getenv("DEPOT_REGISTRY_URL", "registry.depot.dev")

    # Monitoring Configuration
    PROMETHEUS_URL: str = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
    SENTRY_DSN: str | None = os.getenv("SENTRY_DSN")

    # Kubernetes Configuration
    K8S_CONNECTION_POOL_SIZE: int = int(os.getenv("K8S_CONNECTION_POOL_SIZE", "100"))

    # AWS Secrets Manager Configuration
    SECRETS_PREFIX: str = os.getenv("SECRETS_PREFIX", "lazycloud")

    # Ingress Configuration
    BASE_DOMAIN: str = os.getenv("BASE_DOMAIN", "lazycloud.dev")

    # Usage Tracking Configuration
    USAGE_COLLECTION_INTERVAL_HOURS: int = int(
        os.getenv("USAGE_COLLECTION_INTERVAL_HOURS", "1")
    )

    # Billing Configuration
    POLAR_ORGANIZATION_ID: str = os.getenv("POLAR_ORGANIZATION_ID", "")
    POLAR_ACCESS_TOKEN: str = os.getenv("POLAR_ACCESS_TOKEN", "")
    POLAR_WEBHOOK_SECRET: str = os.getenv("POLAR_WEBHOOK_SECRET", "")
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

    # CLI Version (minimum required version) - updated by cli_release.yml workflow
    CLI_VERSION: str = "0.1.19"

    # Email Configuration
    RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
    SUPPORT_EMAIL: str = os.getenv("SUPPORT_EMAIL", "support@lazycloud.dev")

    # Required Environment Variables
    all_environment_variables: List[str] = [
        "ADMIN_API_KEY",
        "REDIS_URL",
        "DB_SECRET_KEY",
        "RESEND_API_KEY",
        "POLAR_ACCESS_TOKEN",
        "POLAR_ORGANIZATION_ID",
        "IS_POLAR_SANDBOX",
        "DEPOT_API_TOKEN",
        "DEPOT_ORG_ID",
        "CLOUDFLARE_API_KEY",
        "CLOUDFLARE_ZONE_ID",
        "CLOUDFLARE_ACCOUNT_ID",
        "PROMETHEUS_URL",
    ]

    production_environment_variables: List[str] = [
        "POLAR_WEBHOOK_SECRET",
    ]

    @model_validator(mode="after")
    def validate_required_env_vars(self: "AppConfig") -> "AppConfig":
        logger.info("Validating required environment variables")
        missing_vars = []
        for var in self.all_environment_variables:
            value = os.getenv(var)
            if not value or value.strip() == "":
                missing_vars.append(var)

        if self.ENV == ENVIRONMENT.PROD:
            # only check production environment variables if we are in production
            for var in self.production_environment_variables:
                value = os.getenv(var)
                if not value or value.strip() == "":
                    missing_vars.append(var)

        if missing_vars:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing_vars)}"
            )

        return self


app_config = AppConfig(ENV=ENVIRONMENT(ENV))
