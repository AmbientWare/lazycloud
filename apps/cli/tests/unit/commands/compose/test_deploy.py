"""Unit tests for compose deploy command

Tests for environment variable extraction and LC_* variable handling.
"""

import pytest

from cli.commands.compose.deploy import (
    _extract_env_variables,
    _extract_env_variables_for_service,
    _extract_service_name_from_lc_var,
    _is_lazycloud_managed_var,
)


@pytest.mark.unit
class TestIsLazyCloudManagedVar:
    """Tests for _is_lazycloud_managed_var function"""

    def test_lc_url_is_managed(self):
        """LC_*_URL variables are managed by LazyCloud"""
        assert _is_lazycloud_managed_var("LC_REDIS_URL") is True
        assert _is_lazycloud_managed_var("LC_API_URL") is True
        assert _is_lazycloud_managed_var("LC_MY_SERVICE_URL") is True

    def test_lc_public_url_is_managed(self):
        """LC_*_PUBLIC_URL variables are managed by LazyCloud"""
        assert _is_lazycloud_managed_var("LC_API_PUBLIC_URL") is True
        assert _is_lazycloud_managed_var("LC_FRONTEND_PUBLIC_URL") is True

    def test_non_lc_prefix_not_managed(self):
        """Variables without LC_ prefix are not managed"""
        assert _is_lazycloud_managed_var("REDIS_URL") is False
        assert _is_lazycloud_managed_var("API_URL") is False
        assert _is_lazycloud_managed_var("DATABASE_URL") is False

    def test_lc_without_url_suffix_not_managed(self):
        """LC_ variables without _URL suffix are not managed"""
        assert _is_lazycloud_managed_var("LC_REDIS_HOST") is False
        assert _is_lazycloud_managed_var("LC_API_KEY") is False
        assert _is_lazycloud_managed_var("LC_CONFIG") is False

    def test_empty_and_edge_cases(self):
        """Edge cases for variable name validation"""
        assert _is_lazycloud_managed_var("") is False
        assert _is_lazycloud_managed_var("LC_") is False
        assert _is_lazycloud_managed_var("LC_URL") is True  # Minimal valid case
        assert _is_lazycloud_managed_var("LC_PUBLIC_URL") is True


@pytest.mark.unit
class TestExtractServiceNameFromLcVar:
    """Tests for _extract_service_name_from_lc_var function"""

    def test_extract_from_url_suffix(self):
        """Extract service name from LC_*_URL variables"""
        assert _extract_service_name_from_lc_var("LC_REDIS_URL") == "redis"
        assert _extract_service_name_from_lc_var("LC_API_URL") == "api"

    def test_extract_from_public_url_suffix(self):
        """Extract service name from LC_*_PUBLIC_URL variables"""
        assert _extract_service_name_from_lc_var("LC_API_PUBLIC_URL") == "api"
        assert _extract_service_name_from_lc_var("LC_FRONTEND_PUBLIC_URL") == "frontend"

    def test_converts_underscores_to_hyphens(self):
        """Service names have underscores converted to hyphens"""
        assert _extract_service_name_from_lc_var("LC_MY_SERVICE_URL") == "my-service"
        assert (
            _extract_service_name_from_lc_var("LC_SOME_LONG_NAME_PUBLIC_URL")
            == "some-long-name"
        )

    def test_converts_to_lowercase(self):
        """Service names are converted to lowercase"""
        assert _extract_service_name_from_lc_var("LC_REDIS_URL") == "redis"
        assert _extract_service_name_from_lc_var("LC_API_URL") == "api"


@pytest.mark.unit
class TestExtractEnvVariables:
    """Tests for _extract_env_variables function"""

    def test_extracts_simple_env_vars(self):
        """Extract simple environment variables from compose"""
        compose_data = {
            "services": {
                "api": {
                    "environment": {
                        "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                        "DEBUG": "true",
                    }
                }
            }
        }
        env_files = {}

        result = _extract_env_variables(compose_data, env_files)

        assert "OPENAI_API_KEY" in result
        assert result["OPENAI_API_KEY"] is None  # Needs user input
        assert result["DEBUG"] == "true"  # Literal value

    def test_skips_lc_url_variables(self):
        """LC_*_URL variables should NOT be added to env_vars"""
        compose_data = {
            "services": {
                "api": {
                    "environment": {
                        "REDIS_URL": "${LC_REDIS_URL:-redis://localhost:6379}",
                        "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                    }
                }
            }
        }
        env_files = {}

        result = _extract_env_variables(compose_data, env_files)

        # REDIS_URL should NOT be in results because it references LC_REDIS_URL
        assert "REDIS_URL" not in result
        # OPENAI_API_KEY should still be there
        assert "OPENAI_API_KEY" in result

    def test_skips_lc_public_url_variables(self):
        """LC_*_PUBLIC_URL variables should NOT be added to env_vars"""
        compose_data = {
            "services": {
                "frontend": {
                    "build": {"context": "./frontend"},
                    "environment": {
                        "API_URL": "${LC_API_PUBLIC_URL:-http://localhost:8000}",
                    },
                }
            }
        }
        env_files = {}

        result = _extract_env_variables(compose_data, env_files)

        # API_URL should NOT be in results because it references LC_API_PUBLIC_URL
        assert "API_URL" not in result

    def test_handles_list_format_environment(self):
        """Handle environment as list format (- KEY=VALUE)"""
        compose_data = {
            "services": {
                "api": {
                    "environment": [
                        "OPENAI_API_KEY=${OPENAI_API_KEY}",
                        "REDIS_URL=${LC_REDIS_URL:-redis://localhost:6379}",
                        "DEBUG=true",
                    ]
                }
            }
        }
        env_files = {}

        result = _extract_env_variables(compose_data, env_files)

        assert "OPENAI_API_KEY" in result
        assert result["OPENAI_API_KEY"] is None
        assert "REDIS_URL" not in result  # LC_ var, should be skipped
        assert result["DEBUG"] == "true"

    def test_handles_lc_var_without_default(self):
        """LC_ variables without default should also be skipped"""
        compose_data = {
            "services": {
                "api": {
                    "environment": {
                        "REDIS_URL": "${LC_REDIS_URL}",
                    }
                }
            }
        }
        env_files = {}

        result = _extract_env_variables(compose_data, env_files)

        assert "REDIS_URL" not in result

    def test_non_lc_var_with_default_still_collected(self):
        """Non-LC variables with defaults should still be collected (default ignored)"""
        compose_data = {
            "services": {
                "api": {
                    "environment": {
                        "OPENAI_MODEL": "${OPENAI_MODEL:-gpt-4o-mini}",
                    }
                }
            }
        }
        env_files = {}

        result = _extract_env_variables(compose_data, env_files)

        # OPENAI_MODEL should be collected (non-LC var)
        assert "OPENAI_MODEL" in result
        assert result["OPENAI_MODEL"] is None  # Needs user input

    def test_real_world_llm_chatbot_example(self):
        """Test with the real llm-chatbot compose.yaml pattern"""
        compose_data = {
            "services": {
                "frontend": {
                    "build": {"context": "./frontend"},
                },
                "api": {
                    "build": {"context": "./api"},
                    "environment": [
                        "OPENAI_API_KEY=${OPENAI_API_KEY}",
                        "OPENAI_MODEL=${OPENAI_MODEL:-gpt-4o-mini}",
                        "REDIS_URL=${LC_REDIS_URL:-redis://redis:6379}",
                    ],
                },
                "redis": {"image": "redis:7-alpine"},
            }
        }
        env_files = {}

        result = _extract_env_variables(compose_data, env_files)

        # Should collect OPENAI_API_KEY and OPENAI_MODEL
        assert "OPENAI_API_KEY" in result
        assert "OPENAI_MODEL" in result
        # Should NOT collect REDIS_URL (references LC_REDIS_URL)
        assert "REDIS_URL" not in result


@pytest.mark.unit
class TestExtractEnvVariablesForService:
    """Tests for _extract_env_variables_for_service function"""

    def test_extracts_only_target_service_vars(self):
        """Extract env vars only for the specified service"""
        compose_data = {
            "services": {
                "api": {
                    "environment": {"API_KEY": "${API_KEY}"},
                },
                "worker": {
                    "environment": {"WORKER_SECRET": "${WORKER_SECRET}"},
                },
            }
        }
        env_files = {}

        result = _extract_env_variables_for_service(
            compose_data, env_files, target_service="api"
        )

        assert "API_KEY" in result
        assert "WORKER_SECRET" not in result

    def test_skips_lc_vars_for_target_service(self):
        """LC_ variables should be skipped even for single service extraction"""
        compose_data = {
            "services": {
                "api": {
                    "environment": [
                        "OPENAI_API_KEY=${OPENAI_API_KEY}",
                        "REDIS_URL=${LC_REDIS_URL:-redis://redis:6379}",
                    ]
                }
            }
        }
        env_files = {}

        result = _extract_env_variables_for_service(
            compose_data, env_files, target_service="api"
        )

        assert "OPENAI_API_KEY" in result
        assert "REDIS_URL" not in result

    def test_returns_empty_for_nonexistent_service(self):
        """Return empty dict for service that doesn't exist"""
        compose_data = {"services": {"api": {}}}
        env_files = {}

        result = _extract_env_variables_for_service(
            compose_data, env_files, target_service="nonexistent"
        )

        assert result == {}

    def test_handles_dict_format_environment(self):
        """Handle environment as dict format"""
        compose_data = {
            "services": {
                "api": {
                    "environment": {
                        "REDIS_URL": "${LC_REDIS_URL:-redis://localhost}",
                        "DEBUG": "true",
                    }
                }
            }
        }
        env_files = {}

        result = _extract_env_variables_for_service(
            compose_data, env_files, target_service="api"
        )

        assert "REDIS_URL" not in result
        assert result["DEBUG"] == "true"
