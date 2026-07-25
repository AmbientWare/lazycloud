from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from provider_aws.capacity_images import (
    AwsCapacityImageClientFactory,
    AwsCapacityImageEc2Client,
    Boto3AwsCapacityImageSharing,
)

_PLATFORM_ACCOUNT = "111111111111"
_CONNECTED_ACCOUNT = "222222222222"
_AMI_ID = "ami-0123456789abcdef0"


@dataclass(slots=True)
class _Ec2:
    owner_id: str
    public: bool = False
    shared_accounts: list[str] = field(default_factory=list)

    def describe_images(self, *, ImageIds: Sequence[str]) -> Mapping[str, object]:
        images: list[Mapping[str, object]] = [
            {"ImageId": image_id, "OwnerId": self.owner_id, "Public": self.public}
            for image_id in ImageIds
        ]
        return {"Images": images}

    def modify_image_attribute(
        self,
        *,
        ImageId: str,
        LaunchPermission: Mapping[str, object],
    ) -> Mapping[str, object]:
        del ImageId
        added = LaunchPermission.get("Add")
        assert isinstance(added, list)
        for entry in cast("list[Mapping[str, object]]", added):
            self.shared_accounts.append(str(entry["UserId"]))
        return {}


def _factory(client: AwsCapacityImageEc2Client) -> AwsCapacityImageClientFactory:
    def build(*, region_name: str) -> AwsCapacityImageEc2Client:
        del region_name
        return client

    return build


def test_capacity_image_sharing_only_shares_privately_owned_platform_images() -> None:
    """Public catalog images and self-owned images are never shared."""
    self_owned = _Ec2(owner_id=_CONNECTED_ACCOUNT)
    platform_owned = _Ec2(owner_id=_PLATFORM_ACCOUNT)
    public_vendor = _Ec2(owner_id="898082745236", public=True)

    same_account = Boto3AwsCapacityImageSharing(_factory(self_owned)).grant_launch_permission(
        region="us-east-1",
        account_id=_CONNECTED_ACCOUNT,
        ami_ids=(_AMI_ID,),
    )
    cross_account = Boto3AwsCapacityImageSharing(_factory(platform_owned)).grant_launch_permission(
        region="us-east-1",
        account_id=_CONNECTED_ACCOUNT,
        ami_ids=(_AMI_ID,),
    )

    public_catalog = Boto3AwsCapacityImageSharing(_factory(public_vendor)).grant_launch_permission(
        region="us-east-1",
        account_id=_CONNECTED_ACCOUNT,
        ami_ids=(_AMI_ID,),
    )

    assert same_account == (_AMI_ID,)
    assert self_owned.shared_accounts == []
    assert public_catalog == (_AMI_ID,)
    assert public_vendor.shared_accounts == []
    assert cross_account == (_AMI_ID,)
    assert platform_owned.shared_accounts == [_CONNECTED_ACCOUNT]
