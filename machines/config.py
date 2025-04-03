from typing import List
import os
from pydantic import BaseModel, field_validator, ConfigDict
from enum import Enum

ENV = os.getenv("ENV", "dev")


class ENVIRONMENT(str, Enum):
    DEV = "dev"
    PROD = "prod"


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)  # Make the config immutable
    ENV: ENVIRONMENT = ENVIRONMENT(ENV)
    ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "")

    # Logging Configuration
    LOG_LEVEL: str = "DEBUG" if ENV == ENVIRONMENT.DEV else "INFO"

    # API Configuration
    PROJECT_NAME: str = "Machines API"
    PROJECT_VERSION: str = "1.0.0"
    API_VERSION: str = os.getenv("API_VERSION", "/v1")

    # Fly.io Configuration
    FLY_API_TOKEN: str = os.getenv("FLY_API_TOKEN", "")
    FLY_ORG_NAME: str = os.getenv("FLY_ORG_NAME", "")

    # Database Configuration
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/machines"
    )

    # Redis Configuration
    UPSTASH_REDIS_REST_URL: str = os.getenv("UPSTASH_REDIS_REST_URL", "")
    UPSTASH_REDIS_REST_TOKEN: str = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

    # Required Environment Variables
    required_env_vars: List[str] = [
        "ADMIN_TOKEN",
        "FLY_API_TOKEN",
        "FLY_ORG_NAME",
        "DATABASE_URL",
        "UPSTASH_REDIS_REST_URL",
        "UPSTASH_REDIS_REST_TOKEN",
    ]

    @field_validator("required_env_vars")
    @classmethod  # Add classmethod decorator for clarity
    def validate_required_env_vars(cls, v: List[str]) -> List[str]:
        missing_vars = [var for var in v if not os.getenv(var)]
        if missing_vars:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing_vars)}"
            )
        return v


app_config = AppConfig(ENV=ENVIRONMENT(ENV))
