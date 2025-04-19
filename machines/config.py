import dotenv
# we load the environment variables from the .env file first so we can use them in rest of the app
dotenv.load_dotenv()

from loguru import logger
from typing import List
import os
from pydantic import BaseModel, ConfigDict, model_validator
from enum import Enum

ENV = os.getenv("ENV", "dev")


class ENVIRONMENT(str, Enum):
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
    PROJECT_NAME: str = "Machines API"
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
    AWS_ROUTE53_ZONE_ID: str = os.getenv("AWS_ROUTE53_ZONE_ID", "")

    # Stripe Configuration
    STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY: str = os.getenv("STRIPE_PUBLISHABLE_KEY", "")

    # Database Configurations
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/machines"
    )
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

    # Required Environment Variables
    required_env_vars: List[str] = [
        "ADMIN_API_KEY",
        "LAZYCLOUD_UPCHARGE",
        "VOLUME_PRICE",
        "DEDICATED_IPV4_PRICE",
        "DATA_EGRESS_PRICE",
        "FLY_API_TOKEN",
        "FLY_ORG_NAME",
        "DATABASE_URL",
        "REDIS_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "AWS_ROUTE53_ZONE_ID",
        "STRIPE_SECRET_KEY",
        "STRIPE_PUBLISHABLE_KEY",
    ]

    @model_validator(mode="after")
    def validate_required_env_vars(self: "AppConfig") -> "AppConfig":
        logger.info("Validating required environment variables")
        missing_vars = [var for var in self.required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing_vars)}"
            )
        return self


app_config = AppConfig(ENV=ENVIRONMENT(ENV))
