"""Tests for ComposeValidator."""

from lazycloud_api.services.compose.validator import ComposeValidator
from shared.models.compose import (
    ComposeFile,
    ComposeNetwork,
    ComposePort,
    ComposeService,
    ComposeVolume,
    DeployConfig,
    ResourceConfig,
    ResourcesConfig,
    ServiceVolume,
)


class TestServiceNameValidation:
    """Tests for service name validation."""

    def test_valid_service_name(self, valid_service_names: list[str]):
        """Test valid Kubernetes-compliant service names pass validation."""
        validator = ComposeValidator()

        for name in valid_service_names:
            compose = ComposeFile(
                services=[ComposeService(name=name, image="nginx:latest")]
            )

            errors, warnings = validator.validate(compose)

            # Should not have any name-related errors
            name_errors = [e for e in errors if e.error_type == "invalid_name"]
            assert len(name_errors) == 0, f"Name '{name}' should be valid"

    def test_invalid_service_name_uppercase(self):
        """Test uppercase service name fails validation."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[ComposeService(name="MyService", image="nginx:latest")]
        )

        errors, warnings = validator.validate(compose)

        assert any(e.error_type == "invalid_name" for e in errors)
        assert any("lowercase" in e.message for e in errors)

    def test_invalid_service_name_underscore(self):
        """Test service name with underscore fails validation."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[ComposeService(name="my_service", image="nginx:latest")]
        )

        errors, warnings = validator.validate(compose)

        assert any(e.error_type == "invalid_name" for e in errors)

    def test_invalid_service_name_starts_with_number(self):
        """Test service name starting with number fails validation."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[ComposeService(name="123service", image="nginx:latest")]
        )

        errors, warnings = validator.validate(compose)

        assert any(e.error_type == "invalid_name" for e in errors)
        assert any("number" in e.message for e in errors)

    def test_invalid_service_name_starts_with_hyphen(self):
        """Test service name starting with hyphen fails validation."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[ComposeService(name="-service", image="nginx:latest")]
        )

        errors, warnings = validator.validate(compose)

        assert any(e.error_type == "invalid_name" for e in errors)

    def test_invalid_service_name_ends_with_hyphen(self):
        """Test service name ending with hyphen fails validation."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[ComposeService(name="service-", image="nginx:latest")]
        )

        errors, warnings = validator.validate(compose)

        assert any(e.error_type == "invalid_name" for e in errors)

    def test_invalid_service_name_too_long(self):
        """Test service name exceeding 63 chars fails validation."""
        validator = ComposeValidator()
        long_name = "a" * 64
        compose = ComposeFile(
            services=[ComposeService(name=long_name, image="nginx:latest")]
        )

        errors, warnings = validator.validate(compose)

        assert any(e.error_type == "invalid_name" for e in errors)
        assert any("63" in e.message for e in errors)

    def test_invalid_service_name_suggestion(self):
        """Test validation provides sanitized name suggestion."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[ComposeService(name="My_Service", image="nginx:latest")]
        )

        errors, warnings = validator.validate(compose)

        name_error = next(e for e in errors if e.error_type == "invalid_name")
        assert "my-service" in name_error.suggestion


class TestVolumeNameValidation:
    """Tests for volume name validation."""

    def test_volume_name_conversion_warning(self):
        """Test warning for volume names that will be converted."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    volumes=[
                        ServiceVolume(type="volume", source="MyVolume", target="/data")
                    ],
                )
            ],
            volumes=[ComposeVolume(name="MyVolume")],
        )

        errors, warnings = validator.validate(compose)

        # Should have warning about name conversion
        name_warnings = [
            w for w in warnings if w.error_type == "name_will_be_converted"
        ]
        assert len(name_warnings) > 0


class TestNetworkValidation:
    """Tests for network configuration validation."""

    def test_multiple_networks_warning(self):
        """Test warning for services using multiple networks."""
        validator = ComposeValidator()
        from shared.models.compose import ServiceNetwork

        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    networks=[
                        ServiceNetwork(name="frontend"),
                        ServiceNetwork(name="backend"),
                    ],
                )
            ],
            networks=[
                ComposeNetwork(name="frontend"),
                ComposeNetwork(name="backend"),
            ],
        )

        errors, warnings = validator.validate(compose)

        # Should have informational warning about multiple networks
        network_warnings = [
            w for w in warnings if w.error_type == "multiple_networks_info"
        ]
        assert len(network_warnings) > 0


class TestResourceValidation:
    """Tests for resource limits/requests validation."""

    def test_valid_memory_values(self):
        """Test valid memory values pass validation."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    deploy=DeployConfig(
                        resources=ResourcesConfig(
                            limits=ResourceConfig(memory="1Gi", cpus="1"),
                            reservations=ResourceConfig(memory="512Mi", cpus="0.5"),
                        )
                    ),
                )
            ]
        )

        errors, warnings = validator.validate(compose)

        memory_errors = [e for e in errors if e.error_type == "invalid_memory"]
        assert len(memory_errors) == 0

    def test_valid_cpu_values(self):
        """Test valid CPU values pass validation."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    deploy=DeployConfig(
                        resources=ResourcesConfig(
                            limits=ResourceConfig(cpus="2", memory="1Gi"),
                        )
                    ),
                )
            ]
        )

        errors, warnings = validator.validate(compose)

        cpu_errors = [e for e in errors if e.error_type == "invalid_cpu"]
        assert len(cpu_errors) == 0

    def test_high_cpu_warning(self):
        """Test warning for very high CPU allocation."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    deploy=DeployConfig(
                        resources=ResourcesConfig(
                            limits=ResourceConfig(cpus="32", memory="1Gi"),
                        )
                    ),
                )
            ]
        )

        errors, warnings = validator.validate(compose)

        # Should have warning about high CPU
        cpu_warnings = [w for w in warnings if w.error_type == "high_cpu"]
        assert len(cpu_warnings) > 0

    def test_limits_with_reservations_no_warning(self):
        """Test no warning when both limits and reservations defined."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    deploy=DeployConfig(
                        resources=ResourcesConfig(
                            limits=ResourceConfig(cpus="1", memory="1Gi"),
                            reservations=ResourceConfig(cpus="0.5", memory="512Mi"),
                        )
                    ),
                )
            ]
        )

        errors, warnings = validator.validate(compose)

        # No warning when both limits and reservations are explicitly defined
        request_warnings = [
            w for w in warnings if w.error_type == "auto_generated_requests"
        ]
        assert len(request_warnings) == 0


class TestPortValidation:
    """Tests for port configuration validation."""

    def test_port_conflict_error(self):
        """Test error when same port is used by multiple services."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    ports=[ComposePort(published=80, target=80)],
                ),
                ComposeService(
                    name="api",
                    image="myapp:latest",
                    ports=[ComposePort(published=80, target=8080)],  # Same host port
                ),
            ]
        )

        errors, warnings = validator.validate(compose)

        port_errors = [e for e in errors if e.error_type == "port_conflict"]
        assert len(port_errors) > 0


class TestImageValidation:
    """Tests for image name validation."""

    def test_missing_image_error(self):
        """Test error when service has no image."""
        validator = ComposeValidator()
        compose = ComposeFile(services=[ComposeService(name="web", image="")])

        errors, warnings = validator.validate(compose)

        image_errors = [e for e in errors if e.error_type == "missing_image"]
        assert len(image_errors) > 0

    def test_valid_image_name(self):
        """Test valid image names pass validation."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[
                ComposeService(name="web", image="nginx:latest"),
                ComposeService(name="api", image="registry.example.com/myapp:v1.2.3"),
            ]
        )

        errors, warnings = validator.validate(compose)

        image_errors = [e for e in errors if e.error_type == "missing_image"]
        assert len(image_errors) == 0


class TestValidateToResult:
    """Tests for validate_to_result method."""

    def test_can_deploy_true_when_no_errors(self):
        """Test can_deploy is True when there are no errors."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[ComposeService(name="web", image="nginx:latest")]
        )

        result = validator.validate_to_result(compose)

        assert result.can_deploy is True
        assert len(result.errors) == 0

    def test_can_deploy_false_when_errors(self):
        """Test can_deploy is False when there are errors."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[ComposeService(name="INVALID_NAME", image="nginx:latest")]
        )

        result = validator.validate_to_result(compose)

        assert result.can_deploy is False
        assert len(result.errors) > 0

    def test_warnings_included_in_result(self):
        """Test warnings are included in validation result."""
        validator = ComposeValidator()
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

        result = validator.validate_to_result(compose)

        # Should have warning about bind mount
        assert len(result.warnings) > 0


class TestVolumeUsageValidation:
    """Tests for volume usage validation."""

    def test_bind_mount_warning(self):
        """Test warning for bind mount usage."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    volumes=[
                        ServiceVolume(
                            type="bind", source="/host/path", target="/container/path"
                        )
                    ],
                )
            ]
        )

        errors, warnings = validator.validate(compose)

        bind_warnings = [w for w in warnings if w.error_type == "bind_mount"]
        assert len(bind_warnings) > 0


class TestComplexScenarios:
    """Tests for complex validation scenarios."""

    def test_multiple_errors_and_warnings(self):
        """Test validation with multiple issues."""
        validator = ComposeValidator()
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="INVALID",  # Invalid name
                    image="",  # Missing image
                    deploy=DeployConfig(
                        resources=ResourcesConfig(
                            limits=ResourceConfig(cpus="32", memory="1Gi"),
                        )
                    ),
                ),
            ]
        )

        errors, warnings = validator.validate(compose)

        # Should have both errors and warnings
        assert len(errors) >= 2  # Invalid name + missing image
        assert len(warnings) >= 1  # High CPU warning

    def test_valid_complex_compose(self, complex_compose_file: ComposeFile):
        """Test validation of complex but valid compose file."""
        validator = ComposeValidator()

        result = validator.validate_to_result(complex_compose_file)

        # Should be deployable (only warnings, no errors)
        assert result.can_deploy is True
