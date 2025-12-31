from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.iam import (
    ControllerIAMRoles,
    EKSIAMRoles,
    PlatformIAMRoles,
)


class SharedIAMRoles(Construct):
    """Shared IAM roles for all environments

    This construct aggregates IAM roles from different service categories:
    - EKS roles: CSI drivers (EBS, EFS)
    - Controller roles: Karpenter
    - Platform roles: External Secrets, ECR
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        # Create IAM role constructs by category
        self.eks_roles = EKSIAMRoles(self, "EKSRoles", config=config)
        self.controller_roles = ControllerIAMRoles(
            self, "ControllerRoles", config=config
        )
        self.platform_roles = PlatformIAMRoles(self, "PlatformRoles", config=config)

    # EKS IAM role properties
    @property
    def ebs_csi_role(self):
        """Get the EBS CSI role"""
        return self.eks_roles.ebs_csi_role

    @property
    def ebs_csi_role_arn(self) -> str:
        """Get the EBS CSI role ARN"""
        return self.eks_roles.ebs_csi_role_arn

    @property
    def efs_csi_role(self):
        """Get the EFS CSI role"""
        return self.eks_roles.efs_csi_role

    # Controller IAM role properties
    @property
    def karpenter_controller_role(self):
        """Get the Karpenter controller role"""
        return self.controller_roles.karpenter_controller_role

    @property
    def karpenter_node_role(self):
        """Get the Karpenter node role"""
        return self.controller_roles.karpenter_node_role

    # Platform IAM role properties
    @property
    def external_secrets_role(self):
        """Get the External Secrets role"""
        return self.platform_roles.external_secrets_role

    @property
    def external_secrets_role_arn(self) -> str:
        """Get the external secrets role ARN"""
        return self.platform_roles.external_secrets_role_arn

    @property
    def ecr_base_role(self):
        """Get the ECR base role"""
        return self.platform_roles.ecr_base_role

    @property
    def ecr_base_role_arn(self) -> str:
        """Get the ECR base role ARN"""
        return self.platform_roles.ecr_base_role_arn

    @property
    def backend_service_user(self):
        """Get the backend service IAM user"""
        return self.platform_roles.backend_service_user

    @property
    def backend_service_user_arn(self) -> str:
        """Get the backend service user ARN"""
        return self.platform_roles.backend_service_user_arn

    @property
    def platform_credentials_secret(self):
        """Get the platform credentials secret"""
        return self.platform_roles.platform_credentials_secret

    @property
    def platform_credentials_secret_arn(self) -> str:
        """Get the platform credentials secret ARN"""
        return self.platform_roles.platform_credentials_secret_arn
