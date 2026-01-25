"""Tests for generator functions."""

import pytest
from models.compose import (
    ComposeFile,
    ComposePort,
    ComposeService,
    ComposeVolume,
    ResourceConfig,
    ResourcesConfig,
    ServiceVolume,
)

from backend.services.k8s.generators.configuration import (
    generate_healthcheck_values,
    generate_service_volumes_values,
)
from backend.services.k8s.generators.networking import (
    generate_ingress_values,
    generate_petname,
    generate_ports_values,
    parse_port_string,
)
from backend.services.k8s.generators.workloads import (
    determine_pull_policy,
    generate_pod_security_context_values,
    generate_resources_values,
    generate_security_context_values,
    parse_image,
)


class TestPortParsing:
    """Tests for port string parsing."""

    def test_parse_single_port(self):
        """Test parsing single port."""
        result = parse_port_string("80")

        assert result.published == 80
        assert result.target == 80
        assert result.protocol == "tcp"

    def test_parse_host_container_port(self):
        """Test parsing host:container port."""
        result = parse_port_string("8080:80")

        assert result.published == 8080
        assert result.target == 80

    def test_parse_ip_host_container_port(self):
        """Test parsing ip:host:container port."""
        result = parse_port_string("127.0.0.1:8080:80")

        assert result.published == 8080
        assert result.target == 80
        assert result.ip == "127.0.0.1"

    def test_parse_port_with_protocol(self):
        """Test parsing port with protocol."""
        result = parse_port_string("53/udp")

        assert result.target == 53
        assert result.protocol == "udp"

    def test_parse_full_format(self):
        """Test parsing full format with ip and protocol."""
        result = parse_port_string("127.0.0.1:8080:80/tcp")

        assert result.published == 8080
        assert result.target == 80
        assert result.protocol == "tcp"

    def test_parse_port_range(self):
        """Test parsing port range."""
        result = parse_port_string("4510-4520")

        assert result.published == "4510-4520"
        assert result.target == "4510-4520"

    def test_parse_invalid_protocol_raises(self):
        """Test invalid protocol raises error."""
        with pytest.raises(ValueError):
            parse_port_string("80/invalid")


class TestPortsValuesGeneration:
    """Tests for ports values generation."""

    def test_generate_ports_from_strings(self):
        """Test generating ports from string formats."""
        ports = ["80", "8080:80"]

        result = generate_ports_values(ports)

        assert len(result) == 2
        assert result[0].port == 80
        assert result[1].port == 8080
        assert result[1].target_port == 80

    def test_generate_ports_from_compose_port(self):
        """Test generating ports from ComposePort objects."""
        ports = [ComposePort(published=8080, target=80, protocol="tcp")]

        result = generate_ports_values(ports)

        assert len(result) == 1
        assert result[0].port == 80
        assert result[0].target_port == 80

    def test_generate_ports_with_udp(self):
        """Test generating ports with UDP protocol."""
        ports = ["53/udp"]

        result = generate_ports_values(ports)

        assert result[0].protocol == "UDP"


class TestImageParsing:
    """Tests for image string parsing."""

    def test_parse_image_with_tag(self):
        """Test parsing image with tag."""
        result = parse_image("nginx:1.25")

        assert result.repository == "nginx"
        assert result.tag == "1.25"

    def test_parse_image_without_tag(self):
        """Test parsing image without tag defaults to latest."""
        result = parse_image("nginx")

        assert result.repository == "nginx"
        assert result.tag == "latest"

    def test_parse_image_with_registry(self):
        """Test parsing image with registry."""
        result = parse_image("registry.example.com/myapp:v1.2.3")

        assert result.repository == "registry.example.com/myapp"
        assert result.tag == "v1.2.3"

    def test_parse_image_with_port(self):
        """Test parsing image with registry port."""
        result = parse_image("localhost:5000/myapp:latest")

        assert result.repository == "localhost:5000/myapp"
        assert result.tag == "latest"


class TestPullPolicyDetermination:
    """Tests for pull policy determination."""

    def test_latest_always_pulls(self):
        """Test 'latest' tag uses Always pull policy."""
        assert determine_pull_policy("latest") == "Always"

    def test_develop_always_pulls(self):
        """Test 'develop' tag uses Always pull policy."""
        assert determine_pull_policy("develop") == "Always"

    def test_production_always_pulls(self):
        """Test 'production' tag uses Always pull policy."""
        assert determine_pull_policy("production") == "Always"

    def test_staging_always_pulls(self):
        """Test 'staging' tag uses Always pull policy."""
        assert determine_pull_policy("staging") == "Always"

    def test_version_tag_if_not_present(self):
        """Test version tags use IfNotPresent."""
        assert determine_pull_policy("v1.2.3") == "IfNotPresent"
        assert determine_pull_policy("1.25") == "IfNotPresent"

    def test_sha_if_not_present(self):
        """Test SHA tags use IfNotPresent."""
        assert determine_pull_policy("abc123def") == "IfNotPresent"


class TestResourcesGeneration:
    """Tests for resource values generation."""

    def test_generate_resources_with_limits_and_requests(self):
        """Test generating resources with both limits and requests."""
        resources_config = ResourcesConfig(
            limits=ResourceConfig(cpus="1", memory="1G"),
            reservations=ResourceConfig(cpus="0.5", memory="512M"),
        )

        result = generate_resources_values(resources_config)

        assert result.limits.cpu == "1"
        assert result.limits.memory == "1Gi"
        # Requests are enforced to minimum thresholds in millicores format
        assert result.requests.cpu == "500m"  # 0.5 cores = 500m
        assert result.requests.memory == "512Mi"

    def test_generate_resources_with_default_reservations(self):
        """Test resources auto-generates requests from limits when no reservations specified."""
        resources_config = ResourcesConfig(
            limits=ResourceConfig(cpus="2", memory="2Gi"),
        )

        result = generate_resources_values(resources_config)

        # Limits should be set correctly
        assert result.limits.cpu == "2"
        assert result.limits.memory == "2Gi"
        # Requests are auto-generated at 50% CPU, 80% memory from limits
        assert result.requests is not None
        assert result.requests.cpu == "1000m"  # 50% of 2 cores = 1 core = 1000m
        assert result.requests.memory == "1638Mi"  # 80% of 2Gi ≈ 1638Mi

    def test_generate_resources_returns_none_when_empty(self):
        """Test returns None when no resources specified."""
        resources_config = ResourcesConfig()

        result = generate_resources_values(resources_config)

        # No limits or reservations = no resources generated
        assert result is None


class TestSecurityContextGeneration:
    """Tests for security context generation."""

    def test_generate_security_context(self):
        """Test generating default security context."""
        result = generate_security_context_values()

        # With gVisor, we use permissive settings
        assert result.run_as_non_root is False
        assert result.run_as_user == 0
        assert result.read_only_root_filesystem is False

    def test_generate_pod_security_context_without_volumes(self):
        """Test pod security context without volumes."""
        result = generate_pod_security_context_values(has_volumes=False)

        # fsGroup should not be set when no volumes
        assert result.fs_group is None

    def test_generate_pod_security_context_with_volumes(self):
        """Test pod security context with volumes."""
        result = generate_pod_security_context_values(has_volumes=True)

        # fsGroup should be set for volume access
        assert result.fs_group == 0


class TestHealthcheckGeneration:
    """Tests for healthcheck values generation."""

    def test_generate_curl_healthcheck(self):
        """Test generating healthcheck from curl command."""
        healthcheck = {
            "test": ["CMD", "curl", "-f", "http://localhost:8080/health"],
            "interval": "30s",
            "timeout": "10s",
            "retries": 3,
        }

        result = generate_healthcheck_values(healthcheck, "api")

        assert result.enabled is True
        assert result.livenessProbe is not None
        assert result.livenessProbe.http_get is not None
        assert result.livenessProbe.http_get.path == "/health"
        assert result.livenessProbe.http_get.port == 8080

    def test_generate_wget_healthcheck(self):
        """Test generating healthcheck from wget command."""
        healthcheck = {
            "test": ["CMD", "wget", "--spider", "http://localhost:3000/status"],
        }

        result = generate_healthcheck_values(healthcheck, "web")

        assert result.livenessProbe.http_get.path == "/status"
        assert result.livenessProbe.http_get.port == 3000

    def test_generate_exec_healthcheck(self):
        """Test generating exec healthcheck."""
        healthcheck = {
            "test": ["CMD", "/bin/health-check", "--verbose"],
        }

        result = generate_healthcheck_values(healthcheck, "api")

        assert result.livenessProbe.exec is not None
        assert "/bin/health-check" in result.livenessProbe.exec.command

    def test_healthcheck_timing_values(self):
        """Test healthcheck timing values are set correctly."""
        healthcheck = {
            "test": ["CMD", "curl", "-f", "http://localhost/health"],
            "interval": "60s",
            "timeout": "5s",
            "retries": 5,
            "start_period": "30s",
        }

        result = generate_healthcheck_values(healthcheck, "api")

        assert result.livenessProbe.period_seconds == 60
        assert result.livenessProbe.timeout_seconds == 5
        assert result.livenessProbe.failure_threshold == 5
        assert result.livenessProbe.initial_delay_seconds == 30

    def test_healthcheck_converts_service_name_to_localhost(self):
        """Test healthcheck converts service name URL to localhost."""
        healthcheck = {
            "test": ["CMD", "curl", "-f", "http://api:8080/health"],
        }

        result = generate_healthcheck_values(healthcheck, "api")

        # Service name should be converted to localhost
        assert result.livenessProbe.http_get.port == 8080


class TestVolumesMountGeneration:
    """Tests for volume mounts generation."""

    def test_generate_volume_mounts(self):
        """Test generating volume mounts from service volumes."""
        volumes = [
            ServiceVolume(type="volume", source="data", target="/app/data"),
        ]
        compose = ComposeFile(
            services=[],
            volumes=[ComposeVolume(name="data")],
        )

        result = generate_service_volumes_values(volumes, compose)

        assert len(result) == 1
        assert result[0].name == "data"
        assert result[0].mount_path == "/app/data"

    def test_generate_volume_mounts_readonly(self):
        """Test generating readonly volume mounts."""
        volumes = [
            ServiceVolume(
                type="volume", source="config", target="/etc/config", read_only=True
            ),
        ]
        compose = ComposeFile(
            services=[],
            volumes=[ComposeVolume(name="config")],
        )

        result = generate_service_volumes_values(volumes, compose)

        assert result[0].read_only is True

    def test_skip_undefined_volumes(self):
        """Test undefined volumes are skipped."""
        volumes = [
            ServiceVolume(type="volume", source="undefined", target="/data"),
        ]
        compose = ComposeFile(services=[], volumes=[])

        result = generate_service_volumes_values(volumes, compose)

        assert len(result) == 0

    def test_skip_external_volumes(self):
        """Test external volumes are skipped."""
        volumes = [
            ServiceVolume(type="volume", source="external-vol", target="/data"),
        ]
        compose = ComposeFile(
            services=[],
            volumes=[ComposeVolume(name="external-vol", external=True)],
        )

        result = generate_service_volumes_values(volumes, compose)

        assert len(result) == 0

    def test_parse_string_volume(self):
        """Test parsing string volume format."""
        volumes = ["data:/app/data"]
        compose = ComposeFile(
            services=[],
            volumes=[ComposeVolume(name="data")],
        )

        result = generate_service_volumes_values(volumes, compose)

        assert len(result) == 1
        assert result[0].name == "data"
        assert result[0].mount_path == "/app/data"


class TestIngressGeneration:
    """Tests for ingress values generation."""

    def test_generate_ingress_with_ports(self):
        """Test generating ingress for service with ports."""
        service = ComposeService(
            name="api",
            image="myapp:latest",
            ports=[ComposePort(published=8080, target=8080, protocol="tcp")],
        )
        deployment_id = "abc123def456"

        result = generate_ingress_values(service, deployment_id)

        assert result is not None
        assert result.enabled is True
        # Hostname should include service name, short deployment ID, and base domain
        assert result.hostname == "api-abc12.lazycloud.dev"

    def test_ingress_not_generated_without_ports(self):
        """Test ingress is not generated for service without ports."""
        service = ComposeService(
            name="worker",
            image="worker:latest",
            ports=None,
        )
        deployment_id = "abc123def456"

        result = generate_ingress_values(service, deployment_id)

        assert result is None

    def test_ingress_uses_nginx_class(self):
        """Test ingress uses NGINX ingress class for Cloudflare Tunnel routing."""
        service = ComposeService(
            name="web",
            image="nginx:latest",
            ports=[ComposePort(published=80, target=80, protocol="tcp")],
        )
        deployment_id = "xyz789ghi012"

        result = generate_ingress_values(service, deployment_id)

        assert result.className == "nginx"

    def test_ingress_hostname_includes_deployment_id(self):
        """Test ingress hostname includes short deployment ID for uniqueness."""
        service = ComposeService(
            name="myservice",
            image="app:latest",
            ports=[ComposePort(published=3000, target=3000, protocol="tcp")],
        )
        deployment_id = "deploy12345"

        result = generate_ingress_values(service, deployment_id)

        assert result.hostname == "myservice-deplo.lazycloud.dev"

    def test_ingress_uses_custom_domain_when_specified(self):
        """Test ingress uses custom domain from service.domain if specified."""
        service = ComposeService(
            name="api",
            image="myapp:latest",
            ports=[ComposePort(published=8080, target=8080, protocol="tcp")],
            domain="api.example.com",
        )
        deployment_id = "abc123def456"

        result = generate_ingress_values(service, deployment_id)

        assert result is not None
        assert result.hostname == "api.example.com"


class TestPetnameGeneration:
    """Tests for petname generation."""

    def test_generate_petname_returns_valid_name(self):
        """Test petname generation returns a valid name."""
        name = generate_petname("my-service")

        assert name is not None
        assert len(name) > 0
        assert "-" in name
        assert name == name.lower()

    def test_generate_petname_format(self):
        """Test petname has expected format (adjective-animal)."""
        name = generate_petname("test-service")

        # Should contain a hyphen (adjective-animal format)
        assert "-" in name
        # Should be lowercase
        assert name == name.lower()
