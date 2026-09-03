from __future__ import annotations

import pytest
from deploy.ami.catalog import NodeImageCatalog, resolve_catalog
from deploy.ami.recipe import host_recipe_sha256


def _catalog(*, recipe_sha256: str) -> NodeImageCatalog:
    return NodeImageCatalog(
        recipe_sha256=recipe_sha256,
        source_revision="a" * 40,
        cpu_ami_ids={"us-east-1": "ami-0123456789abcdef0"},
        gpu_ami_ids={"us-east-1": "ami-0fedcba9876543210"},
    )


def test_release_resolves_only_the_current_host_recipe() -> None:
    expected = host_recipe_sha256()

    outputs = resolve_catalog(_catalog(recipe_sha256=expected))

    assert outputs["node_image_recipe_sha256"] == expected
    assert outputs["cpu_ami_ids"] == '{"us-east-1":"ami-0123456789abcdef0"}'

    with pytest.raises(SystemExit, match="Node Images workflow"):
        resolve_catalog(_catalog(recipe_sha256="b" * 64))
