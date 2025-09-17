"""
Tests for Helm values generator functionality.
"""

import pytest


class TestHelmValuesGenerator:
    """Test cases for HelmValuesGenerator."""

    def test_basic_service_generation(
        self, helm_generator, sample_compose_data, compose_parser
    ):
        """Test basic service generation."""
        compose_file = compose_parser.parse_dict(sample_compose_data)
        helm_values, warnings = helm_generator.generate_values(compose_file)

        assert "services" in helm_values
        assert "api" in helm_values["services"]
        assert "postgres" in helm_values["services"]

        api_service = helm_values["services"]["api"]
        assert api_service["enabled"] is True
        assert api_service["image"]["repository"] == "myapp"
        assert api_service["image"]["tag"] == "latest"

    def test_pull_policy_logic(self, helm_generator):
        """Test pull policy determination logic."""
        # Test latest tag
        latest_image = helm_generator._parse_image("nginx:latest")
        assert latest_image["pullPolicy"] == "Always"

        # Test version tag
        version_image = helm_generator._parse_image("nginx:1.21")
        assert version_image["pullPolicy"] == "IfNotPresent"

        # Test development tags
        dev_image = helm_generator._parse_image("myapp:develop")
        assert dev_image["pullPolicy"] == "Always"

        staging_image = helm_generator._parse_image("myapp:staging")
        assert staging_image["pullPolicy"] == "Always"

    def test_resource_conversion(self, helm_generator, compose_parser):
        """Test resource limits and requests conversion."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "deploy": {
                        "resources": {
                            "limits": {"cpus": "2", "memory": "2Gi"},
                            "reservations": {"cpus": "1", "memory": "1Gi"},
                        }
                    },
                }
            },
        }

        compose_file = compose_parser.parse_dict(compose_dict)
        helm_values, warnings = helm_generator.generate_values(compose_file)

        web_service = helm_values["services"]["web"]
        assert "resources" in web_service
        assert web_service["resources"]["limits"]["cpu"] == "2"
        assert web_service["resources"]["limits"]["memory"] == "2Gi"
        assert web_service["resources"]["requests"]["cpu"] == "1"
        assert web_service["resources"]["requests"]["memory"] == "1Gi"

    def test_memory_conversion(self, helm_generator):
        """Test memory value conversion."""
        # Test Docker format to K8s format
        assert helm_generator._convert_memory_value("1G") == "1Gi"
        assert helm_generator._convert_memory_value("512M") == "512Mi"
        assert helm_generator._convert_memory_value("100K") == "100Ki"

        # Test already K8s format
        assert helm_generator._convert_memory_value("2Gi") == "2Gi"
        assert helm_generator._convert_memory_value("1Ti") == "1Ti"

        # Test bytes conversion
        assert helm_generator._convert_memory_value(1073741824) == "1Gi"  # 1GB in bytes

    def test_hpa_configuration(self, helm_generator, compose_parser):
        """Test HPA configuration generation."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "api": {
                    "image": "myapp:latest",
                    "labels": {
                        "lazycloud.scaling.enabled": "enabled",
                        "lazycloud.scaling.min": "2",
                        "lazycloud.scaling.max": "20",
                        "lazycloud.scaling.cpu": "75",
                        "lazycloud.scaling.memory": "80",
                    },
                }
            },
        }

        compose_file = compose_parser.parse_dict(compose_dict)
        helm_values, warnings = helm_generator.generate_values(compose_file)

        api_service = helm_values["services"]["api"]
        assert "hpa" in api_service
        assert api_service["hpa"]["enabled"] is True
        assert api_service["hpa"]["minReplicas"] == 2
        assert api_service["hpa"]["maxReplicas"] == 20

        # Check metrics
        metrics = api_service["hpa"]["metrics"]
        cpu_metric = next(m for m in metrics if m["resource"]["name"] == "cpu")
        memory_metric = next(m for m in metrics if m["resource"]["name"] == "memory")

        assert cpu_metric["resource"]["target"]["averageUtilization"] == 75
        assert memory_metric["resource"]["target"]["averageUtilization"] == 80

    def test_ingress_configuration(self, helm_generator, compose_parser):
        """Test ingress configuration generation."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "api": {
                    "image": "myapp:latest",
                    "ports": ["8080:8080"],
                    "labels": {
                        "lazycloud.enable_ingress": "true",
                        "lazycloud.hostname_prefix": "api",
                        "lazycloud.tls": "true",
                        "lazycloud.ingress_class": "nginx",
                    },
                },
                "frontend": {
                    "image": "nginx:latest",
                    "ports": ["80:80"],
                    "labels": {
                        "lazycloud.enable_ingress": "true"
                        # No prefix - should get petname
                    },
                },
            },
        }

        compose_file = compose_parser.parse_dict(compose_dict)
        helm_values, warnings = helm_generator.generate_values(compose_file)

        # Test API service with custom prefix
        api_service = helm_values["services"]["api"]
        assert "ingress" in api_service
        assert api_service["ingress"]["enabled"] is True
        assert api_service["ingress"]["hostnamePrefix"] == "api"
        assert api_service["ingress"]["tls"]["enabled"] is True
        assert api_service["ingress"]["className"] == "nginx"

        # Test frontend service with auto-generated petname
        frontend_service = helm_values["services"]["frontend"]
        assert "ingress" in frontend_service
        assert frontend_service["ingress"]["enabled"] is True
        assert frontend_service["ingress"]["hostnamePrefix"]  # Should have a petname
        assert len(frontend_service["ingress"]["hostnamePrefix"]) > 0

    def test_petname_generation(self, helm_generator):
        """Test petname generation produces valid format."""
        # Generate petnames for different services
        petname1 = helm_generator._generate_petname("frontend")
        petname2 = helm_generator._generate_petname("backend")

        # Petnames should be non-empty strings
        assert isinstance(petname1, str)
        assert isinstance(petname2, str)
        assert len(petname1) > 0
        assert len(petname2) > 0

        # Petname should be in format "word-word"
        assert "-" in petname1
        parts = petname1.split("-")
        assert len(parts) >= 2

        # All parts should be alphabetic
        for part in parts:
            assert part.isalpha()

    def test_statefulset_detection(self, helm_generator):
        """Test StatefulSet detection logic."""
        # Test explicit StatefulSet
        labels_explicit = {"lazycloud.statefulset": "true"}
        assert (
            helm_generator._should_be_statefulset(
                labels_explicit, None, "myapp:latest", "test"
            )
            is True
        )

        # Test auto-detection from image name
        assert (
            helm_generator._should_be_statefulset({}, None, "postgres:13", "db") is True
        )
        assert (
            helm_generator._should_be_statefulset({}, None, "redis:latest", "cache")
            is True
        )
        assert (
            helm_generator._should_be_statefulset({}, None, "mysql:8", "database")
            is True
        )

        # Test auto-detection from service name
        assert (
            helm_generator._should_be_statefulset({}, None, "myapp:latest", "postgres")
            is True
        )
        assert (
            helm_generator._should_be_statefulset({}, None, "myapp:latest", "mongodb")
            is True
        )

        # Test regular service (should not be StatefulSet)
        assert (
            helm_generator._should_be_statefulset({}, None, "nginx:latest", "web")
            is False
        )
        assert (
            helm_generator._should_be_statefulset({}, None, "node:16", "api") is False
        )

    def test_configmap_generation(self, helm_generator, compose_parser):
        """Test ConfigMap generation from labels."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "labels": {
                        "lazycloud.config_files": "nginx.conf:/etc/nginx/nginx.conf,app.yaml:/config/app.yaml"
                    },
                }
            },
        }

        compose_file = compose_parser.parse_dict(compose_dict)
        helm_values, warnings = helm_generator.generate_values(compose_file)

        web_service = helm_values["services"]["web"]
        assert "configMaps" in web_service
        assert web_service["configMaps"]["enabled"] is True

        configs = web_service["configMaps"]["configs"]
        assert len(configs) == 2

        # Check first config
        nginx_config = next(c for c in configs if "nginx-conf" in c["name"])
        assert nginx_config["mountPath"] == "/etc/nginx/nginx.conf"
        assert nginx_config["files"] == ["nginx.conf"]

    def test_metrics_configuration(self, helm_generator, compose_parser):
        """Test metrics configuration generation."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "api": {
                    "image": "myapp:latest",
                    "ports": ["8080:8080", "9090:9090"],
                    "labels": {
                        "lazycloud.metrics": "enabled",
                        "lazycloud.metrics.port": "9090",
                        "lazycloud.metrics.path": "/metrics",
                    },
                }
            },
        }

        compose_file = compose_parser.parse_dict(compose_dict)
        helm_values, warnings = helm_generator.generate_values(compose_file)

        api_service = helm_values["services"]["api"]
        assert "metrics" in api_service
        assert api_service["metrics"]["enabled"] is True
        assert api_service["metrics"]["port"] == "9090"
        assert api_service["metrics"]["path"] == "/metrics"

        # Check Prometheus annotations
        annotations = api_service["metrics"]["annotations"]
        assert annotations["prometheus.io/scrape"] == "true"
        assert annotations["prometheus.io/port"] == "9090"
        assert annotations["prometheus.io/path"] == "/metrics"

    def test_restart_policy_mapping(self, helm_generator, compose_parser):
        """Test restart policy mapping."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "web1": {"image": "nginx:latest", "restart": "always"},
                "web2": {"image": "nginx:latest", "restart": "on-failure"},
                "web3": {
                    "image": "nginx:latest",
                    "deploy": {"restart_policy": {"condition": "any"}},
                },
            },
        }

        compose_file = compose_parser.parse_dict(compose_dict)
        helm_values, warnings = helm_generator.generate_values(compose_file)

        assert helm_values["services"]["web1"]["restartPolicy"] == "Always"
        # Even though web2 has "on-failure", Deployments/StatefulSets must use "Always"
        assert helm_values["services"]["web2"]["restartPolicy"] == "Always"
        assert helm_values["services"]["web3"]["restartPolicy"] == "Always"

        # Check that web2 has annotations indicating intended restart policy
        assert (
            helm_values["services"]["web2"]["annotations"][
                "lazycloud.io/intended-restart-policy"
            ]
            == "OnFailure"
        )
        assert (
            "Should be Job"
            in helm_values["services"]["web2"]["annotations"]["lazycloud.io/note"]
        )

    def test_port_protocol_parsing(self, helm_generator):
        """Test port string parsing with protocols."""
        # Test basic formats
        port1 = helm_generator._parse_port_string("80")
        assert port1.published == 80
        assert port1.target == 80
        assert port1.protocol == "tcp"

        port2 = helm_generator._parse_port_string("8080:80")
        assert port2.published == 8080
        assert port2.target == 80
        assert port2.protocol == "tcp"

        # Test with protocols
        port3 = helm_generator._parse_port_string("8080:80/udp")
        assert port3.published == 8080
        assert port3.target == 80
        assert port3.protocol == "udp"

        # Test IP binding
        port4 = helm_generator._parse_port_string("127.0.0.1:8080:80/tcp")
        assert port4.published == 8080
        assert port4.target == 80
        assert port4.protocol == "tcp"
        assert port4.ip == "127.0.0.1"

        # Test invalid protocol
        with pytest.raises(ValueError, match="Invalid protocol"):
            helm_generator._parse_port_string("80:80/rtp")

    def test_volume_generation(self, helm_generator, compose_parser):
        """Test volume generation."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "db": {
                    "image": "postgres:13",
                    "volumes": [
                        "db-data:/var/lib/postgresql/data",
                        "db-config:/config",
                    ],
                }
            },
            "volumes": {"db-data": {}, "db-config": {}},
        }

        compose_file = compose_parser.parse_dict(compose_dict)
        helm_values, warnings = helm_generator.generate_values(compose_file)

        # Check global volumes
        assert "volumes" in helm_values
        assert "db-data" in helm_values["volumes"]
        assert "db-config" in helm_values["volumes"]

        # Check service volumes
        db_service = helm_values["services"]["db"]
        assert "volumes" in db_service
        assert len(db_service["volumes"]) == 2

        data_volume = next(v for v in db_service["volumes"] if v["name"] == "db-data")
        assert data_volume["mountPath"] == "/var/lib/postgresql/data"
        assert data_volume["size"] == "1Gi"

    def test_secret_generation(self, helm_generator, compose_parser):
        """Test secret generation and mounting."""
        compose_dict = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "secrets": [
                        "api-key",
                        {"source": "db-password", "target": "/run/secrets/db_pass"},
                    ],
                }
            },
            "secrets": {
                "api-key": {"file": "./secrets/api_key.txt"},
                "db-password": {"file": "./secrets/db_password.txt"},
            },
        }

        compose_file = compose_parser.parse_dict(compose_dict)
        helm_values, warnings = helm_generator.generate_values(compose_file)

        # Check global secrets
        assert "secrets" in helm_values
        assert "api-key" in helm_values["secrets"]
        assert "db-password" in helm_values["secrets"]

        # Check service secrets
        web_service = helm_values["services"]["web"]
        assert "secrets" in web_service
        assert len(web_service["secrets"]) == 2

        # Check first secret (simple string format)
        api_secret = web_service["secrets"][0]
        assert api_secret["name"] == "api-key"
        assert api_secret["mountPath"] == "/run/secrets/api-key"
        assert api_secret["readOnly"] is True

        # Check second secret (object format with custom target)
        db_secret = web_service["secrets"][1]
        assert db_secret["name"] == "db-password"
        assert db_secret["mountPath"] == "/run/secrets/db_pass"
        assert db_secret["readOnly"] is True
