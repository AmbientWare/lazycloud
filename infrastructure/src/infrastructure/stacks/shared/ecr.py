from aws_cdk import aws_ecr as ecr
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws import MultiServiceECRRepositories

SERVICES = [
    "backend-api",
    "backend-background-workers",
    "backend-crons",
    "web",
]


class SharedECR(Construct):
    def __init__(self, scope: Construct, id: str, config: EnvironmentConfig):
        super().__init__(scope, id)

        self.ecr_repository = MultiServiceECRRepositories(
            self, "ECRRepository", config=config, services=SERVICES
        )

        if config.ecr_regions:
            self._configure_replication(config)

    def _configure_replication(self, config: EnvironmentConfig) -> None:
        destinations = [
            {"region": region, "registryId": config.aws_account_id}
            for region in config.ecr_regions
        ]

        ecr.CfnReplicationConfiguration(
            self,
            "EcrReplication",
            replication_configuration=ecr.CfnReplicationConfiguration.ReplicationConfigurationProperty(
                rules=[
                    ecr.CfnReplicationConfiguration.ReplicationRuleProperty(
                        destinations=[
                            ecr.CfnReplicationConfiguration.ReplicationDestinationProperty(
                                region=dest["region"],
                                registry_id=dest["registryId"],
                            )
                            for dest in destinations
                        ],
                    )
                ]
            ),
        )
