"""Comprehensive tests for ComposeParser."""

from typing import Any

import pytest
from backend.services.compose.parser import ComposeParser
from models.compose import ComposeFile, ComposeService, ServiceVolume
from models.k8s import RestartPolicy

from tests.fixtures.compose_cases import SINGLE_SERVICE_CASES


class TestComposeParserBasic:
    """Basic parsing tests for ComposeParser."""

    def test_parse_simple_compose(self, simple_compose_data: dict[str, Any]):
        """Test parsing a simple compose file."""
        result = ComposeParser.parse_dict(simple_compose_data)

        assert isinstance(result, ComposeFile)
        assert result.version == "3.8"
        assert len(result.services) == 1
        assert result.services[0].name == "web"
        assert result.services[0].image == "nginx:latest"

    def test_parse_complex_compose(self, complex_compose_data: dict[str, Any]):
        """Test parsing a complex compose file with multiple services."""
        result = ComposeParser.parse_dict(complex_compose_data)

        assert len(result.services) == 3
        service_names = [s.name for s in result.services]
        assert "api" in service_names
        assert "postgres" in service_names
        assert "redis" in service_names

    def test_parse_without_version(self):
        """Test parsing compose file without version (modern compose)."""
        data = {
            "services": {
                "web": {"image": "nginx:latest"},
            }
        }

        result = ComposeParser.parse_dict(data)

        assert result.version is None
        assert len(result.services) == 1

    def test_parse_rejects_v2(self):
        """Test parsing rejects version 2 compose files."""
        data = {
            "version": "2.4",
            "services": {
                "web": {"image": "nginx:latest"},
            },
        }

        with pytest.raises(ValueError) as exc_info:
            ComposeParser.parse_dict(data)

        assert "version" in str(exc_info.value).lower()


class TestPortParsing:
    """Tests for port string parsing."""

    def test_parse_simple_port(self):
        """Test parsing simple port (e.g., '80')."""
        result = ComposeParser._parse_port_string("80")

        assert result.published == 80
        assert result.target == 80
        assert result.protocol == "tcp"

    def test_parse_host_container_port(self):
        """Test parsing host:container port (e.g., '8080:80')."""
        result = ComposeParser._parse_port_string("8080:80")

        assert result.published == 8080
        assert result.target == 80
        assert result.protocol == "tcp"

    def test_parse_port_with_ip(self):
        """Test parsing ip:host:container port (e.g., '127.0.0.1:8080:80')."""
        result = ComposeParser._parse_port_string("127.0.0.1:8080:80")

        assert result.published == 8080
        assert result.target == 80
        assert result.protocol == "tcp"

    def test_parse_port_with_tcp_protocol(self):
        """Test parsing port with TCP protocol (e.g., '80/tcp')."""
        result = ComposeParser._parse_port_string("80/tcp")

        assert result.target == 80
        assert result.protocol == "tcp"

    def test_parse_port_with_udp_protocol(self):
        """Test parsing port with UDP protocol (e.g., '53/udp')."""
        result = ComposeParser._parse_port_string("53/udp")

        assert result.target == 53
        assert result.protocol == "udp"

    def test_parse_port_with_sctp_protocol(self):
        """Test parsing port with SCTP protocol (e.g., '5000/sctp')."""
        result = ComposeParser._parse_port_string("5000/sctp")

        assert result.target == 5000
        assert result.protocol == "sctp"

    def test_parse_port_range(self):
        """Test parsing port range (e.g., '4510-4559')."""
        result = ComposeParser._parse_port_string("4510-4559")

        # Should use first port of range
        assert result.published == 4510
        assert result.target == 4510

    def test_parse_invalid_port_returns_none(self):
        """Test parsing invalid port returns None."""
        result = ComposeParser._parse_port_string("invalid")

        assert result is None

    def test_parse_invalid_protocol_returns_none(self):
        """Test parsing port with invalid protocol returns None."""
        result = ComposeParser._parse_port_string("80/invalid")

        assert result is None

    def test_parse_port_integer(self):
        """Test parsing port as integer from compose dict."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "ports": [80],
                },
            }
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].ports[0].target == 80

    def test_parse_port_dict_format(self):
        """Test parsing port in dict format."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "ports": [{"target": 80, "published": 8080}],
                },
            }
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].ports[0].target == 80
        assert result.services[0].ports[0].published == 8080


class TestVolumeParsing:
    """Tests for volume string parsing."""

    def test_parse_named_volume(self):
        """Test parsing named volume (e.g., 'data:/app/data')."""
        result = ComposeParser._parse_volume_string("data:/app/data")

        assert result.type == "volume"
        assert result.source == "data"
        assert result.target == "/app/data"
        assert result.read_only is False

    def test_parse_named_volume_readonly(self):
        """Test parsing named volume with ro flag."""
        result = ComposeParser._parse_volume_string("data:/app/data:ro")

        assert result.type == "volume"
        assert result.source == "data"
        assert result.target == "/app/data"
        assert result.read_only is True

    def test_parse_bind_mount_absolute(self):
        """Test parsing bind mount with absolute path."""
        result = ComposeParser._parse_volume_string("/host/path:/container/path")

        assert result.type == "bind"
        assert result.source == "/host/path"
        assert result.target == "/container/path"

    def test_parse_bind_mount_relative(self):
        """Test parsing bind mount with relative path."""
        result = ComposeParser._parse_volume_string("./local:/container/path")

        assert result.type == "bind"
        assert result.source == "./local"
        assert result.target == "/container/path"

    def test_parse_bind_mount_home(self):
        """Test parsing bind mount with home path."""
        result = ComposeParser._parse_volume_string("~/data:/container/path")

        assert result.type == "bind"
        assert result.source == "~/data"
        assert result.target == "/container/path"

    def test_parse_invalid_volume_returns_none(self):
        """Test parsing invalid volume returns None."""
        result = ComposeParser._parse_volume_string("invalid")

        assert result is None

    def test_volume_filtering_only_named(self):
        """Test that only named volumes from volumes section are included."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "volumes": [
                        "named-vol:/data",
                        "./local:/app",  # bind mount - should be excluded
                        "/host:/container",  # bind mount - should be excluded
                    ],
                },
            },
            "volumes": {
                "named-vol": {},
            },
        }

        result = ComposeParser.parse_dict(data)

        # Only named-vol should be in service volumes (bind mounts filtered out)
        service_volumes = result.services[0].volumes
        assert len(service_volumes) == 1
        assert service_volumes[0].source == "named-vol"


class TestDeployConfigParsing:
    """Tests for deploy configuration parsing."""

    def test_parse_replicas(self):
        """Test parsing replicas configuration."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "deploy": {"replicas": 3},
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].deploy.replicas == 3

    def test_parse_resources(self):
        """Test parsing resource limits and reservations."""
        data = {
            "services": {
                "api": {
                    "image": "myapp:latest",
                    "deploy": {
                        "resources": {
                            "limits": {"cpus": "2", "memory": "2G"},
                            "reservations": {"cpus": "1", "memory": "1G"},
                        },
                    },
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].deploy.resources.limits.cpus == "2"
        assert result.services[0].deploy.resources.limits.memory == "2G"
        assert result.services[0].deploy.resources.reservations.cpus == "1"
        assert result.services[0].deploy.resources.reservations.memory == "1G"


class TestRestartPolicyParsing:
    """Tests for restart policy parsing."""

    def test_parse_restart_always(self):
        """Test parsing restart: always."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "restart": "always",
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].deploy.restart_policy == RestartPolicy.ALWAYS

    def test_parse_restart_on_failure(self):
        """Test parsing restart: on-failure."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "restart": "on-failure",
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].deploy.restart_policy == RestartPolicy.ON_FAILURE

    def test_parse_restart_no(self):
        """Test parsing restart: no."""
        data = {
            "services": {
                "job": {
                    "image": "myapp:latest",
                    "restart": "no",
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].deploy.restart_policy == RestartPolicy.NEVER

    def test_parse_restart_unless_stopped(self):
        """Test parsing restart: unless-stopped (maps to always)."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "restart": "unless-stopped",
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].deploy.restart_policy == RestartPolicy.ALWAYS

    def test_parse_deploy_restart_policy(self):
        """Test parsing deploy.restart_policy format."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "deploy": {"restart_policy": {"condition": "on-failure"}},
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].deploy.restart_policy == RestartPolicy.ON_FAILURE


class TestHealthcheckParsing:
    """Tests for healthcheck configuration parsing."""

    def test_parse_healthcheck_basic(self):
        """Test parsing basic healthcheck configuration."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "healthcheck": {
                        "test": ["CMD", "curl", "-f", "http://localhost/health"],
                        "interval": "30s",
                        "timeout": "10s",
                        "retries": 3,
                    },
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        healthcheck = result.services[0].healthcheck
        assert healthcheck.test == ["CMD", "curl", "-f", "http://localhost/health"]
        assert healthcheck.interval == "30s"
        assert healthcheck.timeout == "10s"
        assert healthcheck.retries == 3

    def test_parse_healthcheck_with_start_period(self):
        """Test parsing healthcheck with start_period."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "healthcheck": {
                        "test": ["CMD", "/health"],
                        "start_period": "60s",
                    },
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].healthcheck.start_period == "60s"

    def test_parse_healthcheck_disabled(self):
        """Test parsing healthcheck with disable: true."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "healthcheck": {
                        "disable": True,
                    },
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].healthcheck.disable is True


class TestLazyCloudLabelsParsing:
    """Tests for LazyCloud-specific labels parsing."""

    def test_parse_lazycloud_ignore(self):
        """Test services with lazycloud.ignore are excluded."""
        data = {
            "services": {
                "web": {"image": "nginx:latest"},
                "helper": {
                    "image": "helper:latest",
                    "labels": {"lazycloud.ignore": "true"},
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        # Only web should be included, helper should be ignored
        assert len(result.services) == 1
        assert result.services[0].name == "web"

    def test_parse_lazycloud_scaling(self, scaling_compose_data: dict[str, Any]):
        """Test parsing lazycloud.scaling labels."""
        result = ComposeParser.parse_dict(scaling_compose_data)

        scaling = result.services[0].scaling
        assert scaling.enabled is True
        assert scaling.min == 2
        assert scaling.max == 10
        assert scaling.cpu == "0.7"
        assert scaling.memory == "0.8"

    def test_parse_lazycloud_domain_from_service_labels(self):
        """Test parsing lazycloud.domain from service-level labels."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "labels": {"lazycloud.domain": "app.example.com"},
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].domain == "app.example.com"

    def test_parse_lazycloud_ingress_domain_from_service_labels(self):
        """Test parsing lazycloud.ingress.domain from service-level labels."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "labels": {"lazycloud.ingress.domain": "api.example.com"},
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].domain == "api.example.com"

    def test_parse_lazycloud_domain_from_deploy_labels(self):
        """Test parsing lazycloud.domain from deploy.labels."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "deploy": {
                        "labels": {"lazycloud.domain": "deploy.example.com"},
                    },
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].domain == "deploy.example.com"

    def test_parse_lazycloud_domain_service_labels_takes_precedence(self):
        """Test service-level labels take precedence over deploy.labels."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "labels": {"lazycloud.domain": "service.example.com"},
                    "deploy": {
                        "labels": {"lazycloud.domain": "deploy.example.com"},
                    },
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        # Service-level labels should take precedence
        assert result.services[0].domain == "service.example.com"

    def test_parse_lazycloud_domain_prefers_domain_over_ingress_domain(self):
        """Test lazycloud.domain takes precedence over lazycloud.ingress.domain."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "labels": {
                        "lazycloud.domain": "primary.example.com",
                        "lazycloud.ingress.domain": "secondary.example.com",
                    },
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].domain == "primary.example.com"

    def test_parse_no_domain_label_returns_none(self):
        """Test service without domain labels has None domain."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "labels": {"other.label": "value"},
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].domain is None


class TestEntrypointParsing:
    """Tests for entrypoint parsing."""

    def test_parse_entrypoint_string(self):
        """Test parsing entrypoint as string."""
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

    def test_parse_entrypoint_list(self):
        """Test parsing entrypoint as list."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "entrypoint": ["/docker-entrypoint.sh", "--verbose"],
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].entrypoint == ["/docker-entrypoint.sh", "--verbose"]

    def test_parse_entrypoint_with_command(self):
        """Test parsing both entrypoint and command."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "entrypoint": ["/docker-entrypoint.sh"],
                    "command": ["nginx", "-g", "daemon off;"],
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].entrypoint == ["/docker-entrypoint.sh"]
        # Command is converted to string when it's a list
        assert "nginx" in result.services[0].command

    def test_parse_no_entrypoint(self):
        """Test service without entrypoint has None."""
        data = {
            "services": {
                "web": {"image": "nginx:latest"},
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].entrypoint is None


class TestWorkingDirParsing:
    """Tests for working_dir parsing."""

    def test_parse_working_dir(self):
        """Test parsing working_dir."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "working_dir": "/app",
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].working_dir == "/app"

    def test_parse_no_working_dir(self):
        """Test service without working_dir has None."""
        data = {
            "services": {
                "web": {"image": "nginx:latest"},
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].working_dir is None


class TestStopGracePeriodParsing:
    """Tests for stop_grace_period parsing."""

    def test_parse_stop_grace_period_seconds(self):
        """Test parsing stop_grace_period with seconds."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "stop_grace_period": "30s",
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].stop_grace_period == 30

    def test_parse_stop_grace_period_minutes(self):
        """Test parsing stop_grace_period with minutes."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "stop_grace_period": "2m",
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].stop_grace_period == 120

    def test_parse_stop_grace_period_combined(self):
        """Test parsing stop_grace_period with combined units."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "stop_grace_period": "1m30s",
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].stop_grace_period == 90

    def test_parse_stop_grace_period_hours(self):
        """Test parsing stop_grace_period with hours."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "stop_grace_period": "1h",
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].stop_grace_period == 3600

    def test_parse_stop_grace_period_none(self):
        """Test service without stop_grace_period has None."""
        data = {
            "services": {
                "web": {"image": "nginx:latest"},
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].stop_grace_period is None


class TestNetworkParsing:
    """Tests for network configuration parsing."""

    def test_parse_networks_list(self):
        """Test parsing networks as list."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "networks": ["frontend", "backend"],
                },
            },
            "networks": ["frontend", "backend"],
        }

        result = ComposeParser.parse_dict(data)

        service_networks = result.services[0].networks
        assert len(service_networks) == 2
        network_names = [n.name for n in service_networks]
        assert "frontend" in network_names
        assert "backend" in network_names

    def test_parse_networks_dict_with_aliases(self):
        """Test parsing networks with aliases."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "networks": {
                        "frontend": {"aliases": ["web-alias", "nginx"]},
                    },
                },
            },
            "networks": ["frontend"],
        }

        result = ComposeParser.parse_dict(data)

        service_networks = result.services[0].networks
        assert len(service_networks) == 1
        assert service_networks[0].name == "frontend"
        assert "web-alias" in service_networks[0].aliases
        assert "nginx" in service_networks[0].aliases


class TestCommandParsing:
    """Tests for command parsing."""

    def test_parse_command_string(self):
        """Test parsing command as string."""
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

    def test_parse_command_list(self):
        """Test parsing command as list (joined to string)."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "command": ["nginx", "-g", "daemon off;"],
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].command == "nginx -g daemon off;"


class TestVolumeDefinitionParsing:
    """Tests for top-level volume definition parsing."""

    def test_parse_simple_volume_definition(self):
        """Test parsing simple volume definition."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "volumes": ["data:/data"],
                },
            },
            "volumes": {"data": {}},
        }

        result = ComposeParser.parse_dict(data)

        assert len(result.volumes) == 1
        assert result.volumes[0].name == "data"

    def test_parse_volume_with_labels(self):
        """Test parsing volume with labels."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "volumes": ["data:/data"],
                },
            },
            "volumes": {
                "data": {
                    "labels": {
                        "lazycloud.volume.size": "20Gi",
                        "lazycloud.volume.shared": "true",
                    },
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.volumes[0].labels["lazycloud.volume.size"] == "20Gi"
        assert result.volumes[0].labels["lazycloud.volume.shared"] == "true"

    def test_parse_external_volume(self):
        """Test parsing external volume."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "volumes": ["data:/data"],
                },
            },
            "volumes": {
                "data": {"external": True},
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.volumes[0].external is True

    def test_parse_null_volume_definition(self):
        """Test parsing volume with null config (simple declaration)."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "volumes": ["data:/data"],
                },
            },
            "volumes": {
                "data": None,
            },
        }

        result = ComposeParser.parse_dict(data)

        assert len(result.volumes) == 1
        assert result.volumes[0].name == "data"


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_parse_empty_services(self):
        """Test parsing with empty services raises error."""
        data = {"services": {}}

        with pytest.raises(ValueError, match="No services defined"):
            ComposeParser.parse_dict(data)

    def test_parse_null_service_skipped(self):
        """Test that null service configs are skipped."""
        data = {
            "services": {
                "web": {"image": "nginx:latest"},
                "null_service": None,
            },
        }

        result = ComposeParser.parse_dict(data)

        assert len(result.services) == 1
        assert result.services[0].name == "web"

    def test_parse_build_without_image_generates_default(self):
        """Test parsing service with build but no image generates default image name."""
        data = {
            "services": {
                "web": {"build": "."},
            },
        }

        result = ComposeParser.parse_dict(data)

        # Parser should auto-generate image name from service name
        assert result.services[0].image == "web:latest"
        assert result.services[0].build == "."

    def test_volumes_filtered_to_used_only(self):
        """Test that volumes list only includes used volumes."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "volumes": ["used-vol:/data"],
                },
            },
            "volumes": {
                "used-vol": {},
                "unused-vol": {},
            },
        }

        result = ComposeParser.parse_dict(data)

        # Only used-vol should be included
        assert len(result.volumes) == 1
        assert result.volumes[0].name == "used-vol"

    def test_networks_filtered_to_used_only(self):
        """Test that networks list only includes used networks."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "networks": ["used-net"],
                },
            },
            "networks": ["used-net", "unused-net"],
        }

        result = ComposeParser.parse_dict(data)

        # Only used-net should be included
        assert len(result.networks) == 1
        assert result.networks[0].name == "used-net"

    def test_parse_service_with_build_and_image(self):
        """Test service with both build and image fields."""
        data = {
            "services": {
                "web": {
                    "image": "myapp:latest",
                    "build": {"context": ".", "dockerfile": "Dockerfile"},
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].image == "myapp:latest"
        assert result.services[0].build is not None

    def test_lazycloud_ignore_false_not_ignored(self):
        """Test service with lazycloud.ignore=false is NOT ignored."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "labels": {"lazycloud.ignore": "false"},
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert len(result.services) == 1
        assert result.services[0].name == "web"

    def test_parse_lazycloud_ingress_domain_from_deploy_labels(self):
        """Test parsing lazycloud.ingress.domain from deploy.labels as fallback."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "deploy": {
                        "labels": {"lazycloud.ingress.domain": "ingress.example.com"},
                    },
                },
            },
        }

        result = ComposeParser.parse_dict(data)

        assert result.services[0].domain == "ingress.example.com"

    def test_parse_networks_dict_format(self):
        """Test parsing networks defined as dict at top level."""
        data = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "networks": ["mynet"],
                },
            },
            "networks": {
                "mynet": {"driver": "bridge"},
            },
        }

        result = ComposeParser.parse_dict(data)

        # Networks defined as dict should work
        assert len(result.networks) == 1
        assert result.networks[0].name == "mynet"


class TestValidateForK8s:
    """Tests for validate_for_k8s method."""

    def test_validate_for_k8s_warns_on_bind_mount_string(self):
        """Test validation warns on bind mount in string format."""
        parser = ComposeParser()

        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    volumes=[
                        ServiceVolume(type="bind", source="/host", target="/container")
                    ],
                )
            ]
        )

        warnings = parser.validate_for_k8s(compose)

        assert len(warnings) > 0
        assert "bind mount" in warnings[0].lower()

    def test_validate_for_k8s_no_warnings_on_named_volumes(self):
        """Test validation has no warnings for named volumes."""
        parser = ComposeParser()

        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    volumes=[
                        ServiceVolume(type="volume", source="data", target="/data")
                    ],
                )
            ]
        )

        warnings = parser.validate_for_k8s(compose)

        assert len(warnings) == 0

    def test_validate_for_k8s_no_volumes(self):
        """Test validation with no volumes."""
        parser = ComposeParser()

        compose = ComposeFile(
            services=[ComposeService(name="web", image="nginx:latest")]
        )

        warnings = parser.validate_for_k8s(compose)

        assert len(warnings) == 0


class TestSharedCasesParsing:
    """Parametrized tests using shared test cases."""

    @pytest.mark.parametrize(
        "case",
        SINGLE_SERVICE_CASES,
        ids=[c["name"] for c in SINGLE_SERVICE_CASES],
    )
    def test_parse_single_service_cases(self, case: dict[str, Any]):
        """Test parsing all single-service cases against expected values."""
        data = case["data"]
        expected = case["expected_parse"]

        result = ComposeParser.parse_dict(data)

        assert len(result.services) == 1
        service = result.services[0]

        # Verify expected fields
        assert service.name == expected["name"]
        assert service.image == expected["image"]

        if "entrypoint" in expected:
            assert service.entrypoint == expected["entrypoint"]
        if "command" in expected:
            assert service.command == expected["command"]
        if "working_dir" in expected:
            assert service.working_dir == expected["working_dir"]
        if "stop_grace_period" in expected:
            assert service.stop_grace_period == expected["stop_grace_period"]
        if "domain" in expected:
            assert service.domain == expected["domain"]
