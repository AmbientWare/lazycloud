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
