from __future__ import annotations

from lazycloud import Image, LinuxArchitecture


def test_sdk_image_authoring_serializes_explicit_linux_architecture() -> None:
    default_image = Image()
    arm_image = Image(architecture=LinuxArchitecture.Arm64)

    assert default_image.spec().architecture is LinuxArchitecture.Amd64
    assert default_image._build_request().model_dump(mode="json")["architecture"] == "amd64"
    assert default_image._verify_request().model_dump(mode="json")["architecture"] == "amd64"
    assert arm_image.spec().architecture is LinuxArchitecture.Arm64
    assert arm_image._build_request().model_dump(mode="json")["architecture"] == "arm64"
