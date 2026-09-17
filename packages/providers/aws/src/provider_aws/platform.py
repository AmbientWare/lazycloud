from __future__ import annotations

import hashlib
import json

from pydantic import ConfigDict, Field, JsonValue, model_validator
from shared.aws_connections import AwsAccountNetwork, AwsRegion

from provider_aws.account_connection import (
    AwsAccountConnectionTarget,
    AwsCapacityIdentity,
    Boto3AwsAccountConnectionValidator,
)


class AwsPlatformBinding(AwsCapacityIdentity):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    provider_ref: str = Field(pattern=r"^aws:[A-Za-z0-9_-]+$")
    networks: dict[AwsRegion, AwsAccountNetwork] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_binding(self) -> AwsPlatformBinding:
        self.validated_scope()
        if self.region not in self.networks:
            raise ValueError("platform AWS region has no network")
        return self

    def validate_provider(self) -> None:
        validator = Boto3AwsAccountConnectionValidator.from_default_chain()
        for region, network in self.networks.items():
            target = AwsAccountConnectionTarget(
                account_id=self.account_id,
                region=region,
                role_arn=self.role_arn,
                external_id=self.external_id,
                node_role_arn=self.node_role_arn,
                node_instance_profile_arn=self.node_instance_profile_arn,
                network=network,
            )
            validator.validate(target)

    def migration_fingerprint(self) -> str:
        networks: dict[str, JsonValue] = {}
        for region, network in self.networks.items():
            subnet_ids: list[JsonValue] = list(sorted(network.subnet_ids))
            network_identity: dict[str, JsonValue] = {
                "vpc_id": network.vpc_id,
                "subnet_ids": subnet_ids,
                "security_group_id": network.security_group_id,
            }
            networks[region] = network_identity
        identity: dict[str, JsonValue] = {
            "account_id": self.account_id,
            "role_arn": self.role_arn,
            "node_role_arn": self.node_role_arn,
            "node_instance_profile_arn": self.node_instance_profile_arn,
            "external_id_sha256": hashlib.sha256(
                self.external_id.get_secret_value().encode()
            ).hexdigest(),
            "networks": networks,
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
