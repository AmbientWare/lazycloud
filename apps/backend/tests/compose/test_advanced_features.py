"""Tests for advanced Docker Compose features and YAML parsing."""

import pytest
import yaml
from backend.services.compose.parser import ComposeParser

from tests.fixtures.compose_cases import (
    ALL_NEW_FIELDS,
    ENTRYPOINT_AND_COMMAND,
    STOP_GRACE_PERIOD_COMBINED,
    STOP_GRACE_PERIOD_MINUTES,
    STOP_GRACE_PERIOD_SECONDS,
    WORKING_DIR,
)


class TestYAMLAnchorsAndAliases:
    """Tests for YAML anchor (&) and alias (*) support."""

    def test_yaml_anchor_basic(self):
        """Test basic YAML anchor and alias are resolved by PyYAML."""
        yaml_str = """
services:
  web:
    image: nginx:latest
    environment: &common-env
      NODE_ENV: production
      LOG_LEVEL: info
  api:
    image: myapp:latest
    environment: *common-env
"""
        data = yaml.safe_load(yaml_str)
        result = ComposeParser.parse_dict(data)

        assert len(result.services) == 2
        service_names = {s.name for s in result.services}
        assert service_names == {"web", "api"}

    def test_yaml_merge_key(self):
        """Test YAML merge key (<<) for inheriting configurations."""
        yaml_str = """
x-common: &common
  restart: always

services:
  web:
    <<: *common
    image: nginx:latest
  api:
    <<: *common
    image: myapp:latest
"""
        data = yaml.safe_load(yaml_str)
        result = ComposeParser.parse_dict(data)

        assert len(result.services) == 2
        # Both services should have restart policy from merge
        for service in result.services:
            assert service.deploy.restart_policy.value == "Always"

    def test_yaml_anchor_with_override(self):
        """Test YAML anchor with field override."""
        yaml_str = """
x-deploy: &default-deploy
  replicas: 2
  resources:
    limits:
      cpus: '1'
      memory: 1G

services:
  web:
    image: nginx:latest
    deploy:
      <<: *default-deploy
      replicas: 3
"""
        data = yaml.safe_load(yaml_str)
        result = ComposeParser.parse_dict(data)

        web = result.services[0]
        # Override should take precedence
        assert web.deploy.replicas == 3
        # But resources should be inherited
        assert web.deploy.resources.limits.memory == "1G"


class TestExtensionFields:
    """Tests for x- extension field handling."""

    def test_x_extension_ignored_at_root(self):
        """Test x- extension fields at root level are ignored."""
        data = {
            "x-logging": {"driver": "json-file"},
            "x-common-env": {"NODE_ENV": "production"},
            "services": {
                "web": {"image": "nginx:latest"},
            },
        }

        result = ComposeParser.parse_dict(data)

        assert len(result.services) == 1
        assert result.services[0].name == "web"

    def test_x_extension_in_service_ignored(self):
        """Test x- extension fields in services are ignored gracefully."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "x-custom-config": {"foo": "bar"},
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert len(result.services) == 1
        assert result.services[0].image == "nginx:latest"

    def test_x_extension_complex_reuse(self):
        """Test x- extension used with YAML anchors for config reuse."""
        yaml_str = """
x-base-service: &base
  restart: always
  deploy:
    resources:
      limits:
        cpus: '0.5'
        memory: 512M

services:
  web:
    <<: *base
    image: nginx:latest
  api:
    <<: *base
    image: api:latest
  worker:
    <<: *base
    image: worker:latest
"""
        data = yaml.safe_load(yaml_str)
        result = ComposeParser.parse_dict(data)

        assert len(result.services) == 3
        for service in result.services:
            assert service.deploy.resources.limits.cpus == "0.5"
            assert service.deploy.resources.limits.memory == "512M"


class TestAdvancedDeployConfig:
    """Tests for advanced deploy configuration options."""

    def test_deploy_update_config(self):
        """Test deploy.update_config is parsed without breaking replicas."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "deploy": {
                        "replicas": 3,
                        "update_config": {
                            "parallelism": 2,
                            "delay": "10s",
                            "failure_action": "rollback",
                            "order": "start-first",
                        },
                    },
                },
            },
        }

        result = ComposeParser.parse_dict(data)
        assert result.services[0].deploy.replicas == 3

    def test_deploy_rollback_config(self):
        """Test deploy.rollback_config is parsed without breaking replicas."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "deploy": {
                        "replicas": 2,
                        "rollback_config": {
                            "parallelism": 1,
                            "delay": "5s",
                        },
                    },
                },
            },
        }

        result = ComposeParser.parse_dict(data)
        assert result.services[0].deploy.replicas == 2


class TestAdvancedVolumeConfig:
    """Tests for advanced volume configurations."""

    def test_volume_with_driver_opts(self):
        """Test volume with driver options parses volume name correctly."""
        data = {
            "services": {
                "db": {
                    "image": "postgres:15",
                    "volumes": ["pgdata:/var/lib/postgresql/data"],
                },
            },
            "volumes": {
                "pgdata": {
                    "driver": "local",
                    "driver_opts": {
                        "type": "nfs",
                        "o": "addr=10.0.0.1,rw",
                        "device": ":/path/to/dir",
                    },
                },
            },
        }

        result = ComposeParser.parse_dict(data)
        assert len(result.volumes) == 1
        assert result.volumes[0].name == "pgdata"

    def test_volume_long_syntax(self):
        """Test volume long syntax parses read_only correctly."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "volumes": [
                        {
                            "type": "volume",
                            "source": "mydata",
                            "target": "/data",
                            "read_only": True,
                            "volume": {"nocopy": True},
                        },
                    ],
                },
            },
            "volumes": {"mydata": {}},
        }

        result = ComposeParser.parse_dict(data)
        assert len(result.services) == 1
        assert result.services[0].volumes[0].read_only is True


class TestAdvancedNetworkConfig:
    """Tests for advanced network configurations."""

    def test_service_network_with_aliases(self):
        """Test service network aliases are parsed correctly."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "networks": {
                        "backend": {
                            "ipv4_address": "172.28.0.10",
                            "aliases": ["web-service"],
                        },
                    },
                },
            },
            "networks": {"backend": {}},
        }

        result = ComposeParser.parse_dict(data)
        assert len(result.services) == 1
        assert result.services[0].networks[0].aliases == ["web-service"]


class TestCommandAndEntrypoint:
    """Tests for command and entrypoint variations."""

    def test_command_string(self):
        """Test command as single string."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "command": "nginx -g 'daemon off;'",
                },
            },
        }

        result = ComposeParser.parse_dict(data)
        assert result.services[0].command == "nginx -g 'daemon off;'"

    def test_command_list(self):
        """Test command as list (shell form)."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "command": ["nginx", "-g", "daemon off;"],
                },
            },
        }

        result = ComposeParser.parse_dict(data)
        assert "nginx" in result.services[0].command
        assert "daemon off;" in result.services[0].command

    def test_command_with_shell_expansion(self):
        """Test command with shell variable expansion syntax."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "command": "sh -c 'echo $$HOME && nginx'",
                },
            },
        }

        result = ComposeParser.parse_dict(data)
        assert "$$HOME" in result.services[0].command

    def test_multiline_command(self):
        """Test multiline command using YAML literal block."""
        yaml_str = """
services:
  web:
    image: python:3.11
    command: |
      python -c "
      import time
      while True:
          print('running')
          time.sleep(1)
      "
"""
        data = yaml.safe_load(yaml_str)
        result = ComposeParser.parse_dict(data)

        assert "import time" in result.services[0].command

    def test_entrypoint_string(self):
        """Test entrypoint as single string."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "entrypoint": "/docker-entrypoint.sh",
                },
            },
        }

        result = ComposeParser.parse_dict(data)
        assert result.services[0].entrypoint == "/docker-entrypoint.sh"

    def test_entrypoint_list(self):
        """Test entrypoint as list."""
        data = {
            "services": {
                "web": {
                    "image": "node:18",
                    "entrypoint": ["node", "--inspect"],
                },
            },
        }

        result = ComposeParser.parse_dict(data)
        assert result.services[0].entrypoint == ["node", "--inspect"]

    def test_entrypoint_with_command(self):
        """Test entrypoint combined with command (K8s command + args pattern)."""
        data = {
            "services": {
                "web": {
                    "image": "python:3.11",
                    "entrypoint": ["python"],
                    "command": ["-m", "http.server", "8000"],
                },
            },
        }

        result = ComposeParser.parse_dict(data)
        assert result.services[0].entrypoint == ["python"]
        # Command list is joined to string
        assert "-m" in result.services[0].command
        assert "http.server" in result.services[0].command


class TestWorkingDirAndGracePeriod:
    """Tests for working_dir and stop_grace_period configurations."""

    def test_working_dir(self):
        """Test working_dir is parsed."""
        data = {
            "services": {
                "web": {
                    "image": "node:18",
                    "working_dir": "/app/src",
                },
            },
        }

        result = ComposeParser.parse_dict(data)
        assert result.services[0].working_dir == "/app/src"

    def test_stop_grace_period_various_formats(self):
        """Test stop_grace_period with various duration formats."""
        test_cases = [
            ("10s", 10),
            ("1m", 60),
            ("1m30s", 90),
            ("2h", 7200),
        ]

        for duration, expected_seconds in test_cases:
            data = {
                "services": {
                    "web": {
                        "image": "nginx:latest",
                        "stop_grace_period": duration,
                    },
                },
            }

            result = ComposeParser.parse_dict(data)
            assert result.services[0].stop_grace_period == expected_seconds, (
                f"Failed for {duration}"
            )

    def test_combined_service_config(self):
        """Test service with entrypoint, command, working_dir, and stop_grace_period."""
        data = {
            "services": {
                "app": {
                    "image": "myapp:latest",
                    "entrypoint": ["/entrypoint.sh"],
                    "command": ["start", "--port", "8080"],
                    "working_dir": "/app",
                    "stop_grace_period": "30s",
                },
            },
        }

        result = ComposeParser.parse_dict(data)
        service = result.services[0]

        assert service.entrypoint == ["/entrypoint.sh"]
        assert "start" in service.command
        assert service.working_dir == "/app"
        assert service.stop_grace_period == 30


class TestSharedCasesAdvanced:
    """Tests using shared test cases for advanced features."""

    @pytest.mark.parametrize(
        "case",
        [
            WORKING_DIR,
            STOP_GRACE_PERIOD_SECONDS,
            STOP_GRACE_PERIOD_MINUTES,
            STOP_GRACE_PERIOD_COMBINED,
            ENTRYPOINT_AND_COMMAND,
            ALL_NEW_FIELDS,
        ],
        ids=[
            "working_dir",
            "stop_grace_period_seconds",
            "stop_grace_period_minutes",
            "stop_grace_period_combined",
            "entrypoint_and_command",
            "all_new_fields",
        ],
    )
    def test_parse_shared_cases(self, case):
        """Test parsing shared cases against expected values."""
        data = case["data"]
        expected = case["expected_parse"]

        result = ComposeParser.parse_dict(data)

        assert len(result.services) == 1
        service = result.services[0]

        assert service.name == expected["name"]
        assert service.image == expected["image"]
        assert service.entrypoint == expected["entrypoint"]
        assert service.command == expected["command"]
        assert service.working_dir == expected["working_dir"]
        assert service.stop_grace_period == expected["stop_grace_period"]
