"""Tests for ComposeDiffChecker."""

from models.billing import STORAGE_CLASS_EBS, STORAGE_CLASS_EFS
from models.compose import (
    ComposeFile,
    ComposeNetwork,
    ComposePort,
    ComposeService,
    ComposeVolume,
    DeployConfig,
    ResourceConfig,
    ResourcesConfig,
    ServiceNetwork,
    ServiceVolume,
)

from backend.services.compose.diff_checker import (
    ComposeDiffChecker,
    detect_storage_type_changes,
    get_shared_volumes,
)


class TestComposeDiffCheckerBasic:
    """Basic tests for ComposeDiffChecker."""

    def test_compare_identical_compose_files(self):
        """Test comparing identical compose files shows no changes."""
        compose = ComposeFile(
            services=[ComposeService(name="web", image="nginx:latest")]
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(compose, compose)

        assert diff.has_changes() is False
        assert len(diff.services.added) == 0
        assert len(diff.services.removed) == 0
        assert len(diff.services.modified) == 0

    def test_compare_new_deployment(self):
        """Test comparing with None current (new deployment)."""
        new_compose = ComposeFile(
            services=[
                ComposeService(name="web", image="nginx:latest"),
                ComposeService(name="api", image="myapp:latest"),
            ]
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(None, new_compose)

        assert diff.has_changes() is True
        assert len(diff.services.added) == 2


class TestServiceDiffDetection:
    """Tests for service diff detection."""

    def test_detect_added_service(
        self,
        current_compose_file: ComposeFile,
        new_compose_file: ComposeFile,
    ):
        """Test detection of added services."""
        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current_compose_file, new_compose_file)

        # 'worker' is added
        added_names = [s.get("name") for s in diff.services.added]
        assert "worker" in added_names

    def test_detect_removed_service(
        self,
        current_compose_file: ComposeFile,
        new_compose_file: ComposeFile,
    ):
        """Test detection of removed services."""
        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current_compose_file, new_compose_file)

        # 'api' is removed
        removed_names = [s.get("name") for s in diff.services.removed]
        assert "api" in removed_names

    def test_detect_modified_service_image(
        self,
        current_compose_file: ComposeFile,
        new_compose_file: ComposeFile,
    ):
        """Test detection of modified service image."""
        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current_compose_file, new_compose_file)

        # 'web' image changed from nginx:1.20 to nginx:1.21
        assert "web" in diff.services.modified
        web_changes = diff.services.modified["web"]
        assert "image" in web_changes

    def test_detect_modified_service_ports(self):
        """Test detection of modified service ports."""
        current = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    ports=[ComposePort(published=80, target=80)],
                )
            ]
        )

        new = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    ports=[
                        ComposePort(published=80, target=80),
                        ComposePort(published=443, target=443),
                    ],
                )
            ]
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        assert "web" in diff.services.modified
        assert "ports" in diff.services.modified["web"]

    def test_detect_modified_service_command(self):
        """Test detection of modified service command."""
        current = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    command="nginx",
                )
            ]
        )

        new = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    command="nginx -g daemon off;",
                )
            ]
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        assert "web" in diff.services.modified
        assert "command" in diff.services.modified["web"]

    def test_detect_modified_deploy_config(self):
        """Test detection of modified deploy configuration."""
        current = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    deploy=DeployConfig(replicas=1),
                )
            ]
        )

        new = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    deploy=DeployConfig(replicas=3),
                )
            ]
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        assert "web" in diff.services.modified
        assert "deploy" in diff.services.modified["web"]

    def test_detect_modified_resource_limits(self):
        """Test detection of modified resource limits."""
        current = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    deploy=DeployConfig(
                        resources=ResourcesConfig(
                            limits=ResourceConfig(cpus="1", memory="1Gi")
                        )
                    ),
                )
            ]
        )

        new = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    deploy=DeployConfig(
                        resources=ResourcesConfig(
                            limits=ResourceConfig(cpus="2", memory="2Gi")
                        )
                    ),
                )
            ]
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        assert "web" in diff.services.modified

    def test_detect_modified_stop_grace_period(self):
        """Test detection of modified stop grace period."""
        current = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    stop_grace_period=30,
                )
            ]
        )

        new = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    stop_grace_period=60,
                )
            ]
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        assert "web" in diff.services.modified
        assert "stop_grace_period" in diff.services.modified["web"]

    def test_detect_modified_entrypoint(self):
        """Test detection of modified entrypoint."""
        current = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    entrypoint="/old-entrypoint.sh",
                )
            ]
        )

        new = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    entrypoint="/new-entrypoint.sh",
                )
            ]
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        assert "web" in diff.services.modified
        assert "entrypoint" in diff.services.modified["web"]

    def test_detect_modified_working_dir(self):
        """Test detection of modified working_dir."""
        current = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    working_dir="/old",
                )
            ]
        )

        new = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    working_dir="/new",
                )
            ]
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        assert "web" in diff.services.modified
        assert "working_dir" in diff.services.modified["web"]

    def test_detect_modified_service_domain(self):
        """Test detection of modified service domain."""
        current = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    domain="old.example.com",
                )
            ]
        )

        new = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    domain="new.example.com",
                )
            ]
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        assert "web" in diff.services.modified

    def test_detect_added_service_domain(self):
        """Test detection of added domain to service."""
        current = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    domain=None,
                )
            ]
        )

        new = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    domain="app.example.com",
                )
            ]
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        assert "web" in diff.services.modified


class TestVolumeDiffDetection:
    """Tests for volume diff detection."""

    def test_detect_added_volume(
        self,
        current_compose_file: ComposeFile,
        new_compose_file: ComposeFile,
    ):
        """Test detection of added volumes."""
        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current_compose_file, new_compose_file)

        # 'cache' volume is added
        added_names = [v.get("name") for v in diff.volumes.added]
        assert "cache" in added_names

    def test_detect_removed_volume(self):
        """Test detection of removed volumes."""
        current = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    volumes=[ServiceVolume(type="volume", source="data", target="/d")],
                )
            ],
            volumes=[ComposeVolume(name="data"), ComposeVolume(name="cache")],
        )

        new = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    volumes=[ServiceVolume(type="volume", source="data", target="/d")],
                )
            ],
            volumes=[ComposeVolume(name="data")],
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        removed_names = [v.get("name") for v in diff.volumes.removed]
        assert "cache" in removed_names

    def test_detect_modified_volume_labels(self):
        """Test detection of modified volume labels."""
        current = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    volumes=[ServiceVolume(type="volume", source="data", target="/d")],
                )
            ],
            volumes=[ComposeVolume(name="data", labels={"size": "10Gi"})],
        )

        new = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    volumes=[ServiceVolume(type="volume", source="data", target="/d")],
                )
            ],
            volumes=[ComposeVolume(name="data", labels={"size": "20Gi"})],
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        assert "data" in diff.volumes.modified


class TestNetworkDiffDetection:
    """Tests for network diff detection."""

    def test_detect_added_network(self):
        """Test detection of added networks."""
        current = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    networks=[ServiceNetwork(name="frontend")],
                )
            ],
            networks=[ComposeNetwork(name="frontend")],
        )

        new = ComposeFile(
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

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        added_names = [n.get("name") for n in diff.networks.added]
        assert "backend" in added_names

    def test_detect_removed_network(self):
        """Test detection of removed networks."""
        current = ComposeFile(
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

        new = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    networks=[ServiceNetwork(name="frontend")],
                )
            ],
            networks=[ComposeNetwork(name="frontend")],
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        removed_names = [n.get("name") for n in diff.networks.removed]
        assert "backend" in removed_names


class TestSharedVolumeDetection:
    """Tests for shared volume detection."""

    def test_detect_shared_volume(self, shared_volume_compose_file: ComposeFile):
        """Test detecting volumes used by multiple services."""
        shared = get_shared_volumes(shared_volume_compose_file)

        assert "shared-data" in shared

    def test_single_service_volume_not_shared(self):
        """Test single-service volumes are not marked as shared."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="db",
                    image="postgres:15",
                    volumes=[
                        ServiceVolume(type="volume", source="data", target="/data")
                    ],
                )
            ],
            volumes=[ComposeVolume(name="data")],
        )

        shared = get_shared_volumes(compose)

        assert "data" not in shared

    def test_bind_mount_not_counted(self):
        """Test bind mounts are not counted for sharing."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    volumes=[
                        ServiceVolume(
                            type="bind", source="./local", target="/app"
                        )  # bind mount
                    ],
                ),
                ComposeService(
                    name="api",
                    image="myapp:latest",
                    volumes=[
                        ServiceVolume(
                            type="bind", source="./local", target="/app"
                        )  # same bind mount
                    ],
                ),
            ],
        )

        shared = get_shared_volumes(compose)

        # Bind mounts should not be detected as shared volumes
        assert len(shared) == 0


class TestStorageTypeChangeDetection:
    """Tests for storage type change detection."""

    def test_detect_ebs_to_efs_change(self):
        """Test detecting EBS to EFS storage type change."""
        # Compose where volume is now shared
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="api",
                    image="myapp:latest",
                    volumes=[
                        ServiceVolume(type="volume", source="data", target="/data")
                    ],
                ),
                ComposeService(
                    name="worker",
                    image="worker:latest",
                    volumes=[
                        ServiceVolume(type="volume", source="data", target="/data")
                    ],
                ),
            ],
            volumes=[ComposeVolume(name="data")],
        )

        # Existing PVC with EBS storage class
        existing_pvcs = {"data": STORAGE_CLASS_EBS}

        changes = detect_storage_type_changes(compose, existing_pvcs)

        assert len(changes) == 1
        assert changes[0].volume_name == "data"
        assert changes[0].old_storage_class == STORAGE_CLASS_EBS
        assert changes[0].new_storage_class == STORAGE_CLASS_EFS

    def test_detect_efs_to_ebs_change(self):
        """Test detecting EFS to EBS storage type change."""
        # Compose where volume is no longer shared
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="db",
                    image="postgres:15",
                    volumes=[
                        ServiceVolume(type="volume", source="data", target="/data")
                    ],
                )
            ],
            volumes=[ComposeVolume(name="data")],
        )

        # Existing PVC with EFS storage class
        existing_pvcs = {"data": STORAGE_CLASS_EFS}

        changes = detect_storage_type_changes(compose, existing_pvcs)

        assert len(changes) == 1
        assert changes[0].old_storage_class == STORAGE_CLASS_EFS
        assert changes[0].new_storage_class == STORAGE_CLASS_EBS

    def test_no_change_when_same_storage_class(self):
        """Test no change detected when storage class is the same."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="db",
                    image="postgres:15",
                    volumes=[
                        ServiceVolume(type="volume", source="data", target="/data")
                    ],
                )
            ],
            volumes=[ComposeVolume(name="data")],
        )

        # Same storage class
        existing_pvcs = {"data": STORAGE_CLASS_EBS}

        changes = detect_storage_type_changes(compose, existing_pvcs)

        assert len(changes) == 0

    def test_no_change_for_new_volumes(self):
        """Test no storage type change for new volumes."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="db",
                    image="postgres:15",
                    volumes=[
                        ServiceVolume(type="volume", source="new-data", target="/data")
                    ],
                )
            ],
            volumes=[ComposeVolume(name="new-data")],
        )

        # No existing PVCs
        existing_pvcs = {}

        changes = detect_storage_type_changes(compose, existing_pvcs)

        assert len(changes) == 0

    def test_label_overrides_shared_detection(self):
        """Test lazycloud.volume.shared label overrides auto-detection."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="db",
                    image="postgres:15",
                    volumes=[
                        ServiceVolume(type="volume", source="data", target="/data")
                    ],
                )  # Single service
            ],
            volumes=[
                ComposeVolume(
                    name="data", labels={"lazycloud.volume.shared": "true"}
                )  # Forced shared
            ],
        )

        existing_pvcs = {"data": STORAGE_CLASS_EBS}

        changes = detect_storage_type_changes(compose, existing_pvcs)

        # Should detect change because label forces EFS
        assert len(changes) == 1
        assert changes[0].new_storage_class == STORAGE_CLASS_EFS


class TestDiffHasChanges:
    """Tests for has_changes method."""

    def test_has_changes_with_added_services(self):
        """Test has_changes returns True when services added."""
        current = ComposeFile(
            services=[ComposeService(name="web", image="nginx:latest")]
        )

        new = ComposeFile(
            services=[
                ComposeService(name="web", image="nginx:latest"),
                ComposeService(name="api", image="myapp:latest"),
            ]
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        assert diff.has_changes() is True

    def test_has_changes_with_removed_services(self):
        """Test has_changes returns True when services removed."""
        current = ComposeFile(
            services=[
                ComposeService(name="web", image="nginx:latest"),
                ComposeService(name="api", image="myapp:latest"),
            ]
        )

        new = ComposeFile(services=[ComposeService(name="web", image="nginx:latest")])

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        assert diff.has_changes() is True

    def test_has_changes_with_modified_services(self):
        """Test has_changes returns True when services modified."""
        current = ComposeFile(services=[ComposeService(name="web", image="nginx:1.20")])

        new = ComposeFile(services=[ComposeService(name="web", image="nginx:1.21")])

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(current, new)

        assert diff.has_changes() is True

    def test_has_changes_false_when_identical(self):
        """Test has_changes returns False when identical."""
        compose = ComposeFile(
            services=[ComposeService(name="web", image="nginx:latest")]
        )

        checker = ComposeDiffChecker()
        diff = checker.compare_compose_files(compose, compose)

        assert diff.has_changes() is False
