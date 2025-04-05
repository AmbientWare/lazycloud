from typing import List
import os
from pydantic import BaseModel, field_validator, ConfigDict, model_validator
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

    # Fly.io Configuration
    FLY_API_TOKEN: str = os.getenv("FLY_API_TOKEN", "")
    FLY_ORG_NAME: str = os.getenv("FLY_ORG_NAME", "")

    # Database Configurations
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/machines"
    )
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

    # Required Environment Variables
    required_env_vars: List[str] = [
        "ADMIN_API_KEY",
        "FLY_API_TOKEN",
        "FLY_ORG_NAME",
        "DATABASE_URL",
        "REDIS_URL",
    ]

    @model_validator(mode='after')
    def validate_required_env_vars(self: 'AppConfig') -> 'AppConfig':
        print("Validating required environment variables")
        missing_vars = [var for var in self.required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing_vars)}"
            )
        return self


app_config = AppConfig(ENV=ENVIRONMENT(ENV))
