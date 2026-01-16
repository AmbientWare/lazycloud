"""Unit tests for compose deploy command

Tests for environment variable extraction.
"""

import pytest

from cli.commands.compose.deploy import (
    _extract_env_variables,
    _extract_env_variables_for_service,
)


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

    def test_extracts_variable_references(self):
        """Variables referencing other variables should be collected"""
        compose_data = {
            "services": {
                "api": {
                    "environment": {
                        "REDIS_URL": "${REDIS_URL:-redis://localhost:6379}",
                        "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                    }
                }
            }
        }
        env_files = {}

        result = _extract_env_variables(compose_data, env_files)

        # Both variables should be collected
        assert "REDIS_URL" in result
        assert "OPENAI_API_KEY" in result

    def test_handles_list_format_environment(self):
        """Handle environment as list format (- KEY=VALUE)"""
        compose_data = {
            "services": {
                "api": {
                    "environment": [
                        "OPENAI_API_KEY=${OPENAI_API_KEY}",
                        "REDIS_URL=${REDIS_URL:-redis://localhost:6379}",
                        "DEBUG=true",
                    ]
                }
            }
        }
        env_files = {}

        result = _extract_env_variables(compose_data, env_files)

        assert "OPENAI_API_KEY" in result
        assert result["OPENAI_API_KEY"] is None
        assert "REDIS_URL" in result
        assert result["DEBUG"] == "true"

    def test_var_with_default_still_collected(self):
        """Variables with defaults should still be collected (default ignored for production)"""
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

        # OPENAI_MODEL should be collected
        assert "OPENAI_MODEL" in result
        assert result["OPENAI_MODEL"] is None  # Needs user input

    def test_real_world_llm_chatbot_example(self):
        """Test with a real-world compose.yaml pattern"""
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
                        "REDIS_URL=redis://redis:6379",
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
        # REDIS_URL has a literal value, should be extracted as-is
        assert result["REDIS_URL"] == "redis://redis:6379"


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

    def test_extracts_vars_for_target_service(self):
        """Variables should be extracted for the target service"""
        compose_data = {
            "services": {
                "api": {
                    "environment": [
                        "OPENAI_API_KEY=${OPENAI_API_KEY}",
                        "REDIS_URL=redis://redis:6379",
                    ]
                }
            }
        }
        env_files = {}

        result = _extract_env_variables_for_service(
            compose_data, env_files, target_service="api"
        )

        assert "OPENAI_API_KEY" in result
        assert result["REDIS_URL"] == "redis://redis:6379"

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
                        "REDIS_URL": "redis://localhost:6379",
                        "DEBUG": "true",
                    }
                }
            }
        }
        env_files = {}

        result = _extract_env_variables_for_service(
            compose_data, env_files, target_service="api"
        )

        assert result["REDIS_URL"] == "redis://localhost:6379"
        assert result["DEBUG"] == "true"
