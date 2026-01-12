import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_imagebuilder as imagebuilder,
    CfnOutput,
)
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig


class GvisorAmiConstruct(Construct):
    """EC2 Image Builder pipeline for AL2023 with gVisor pre-installed.

    This creates an AMI with gVisor (runsc) and containerd-shim-runsc-v1
    already installed and configured, eliminating race conditions during
    node bootstrap.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: EnvironmentConfig,
        vpc: ec2.Vpc,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config

        # IAM role for Image Builder instances
        self.instance_role = iam.Role(
            self,
            "ImageBuilderRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "EC2InstanceProfileForImageBuilder"
                ),
            ],
        )

        self.instance_profile = iam.CfnInstanceProfile(
            self,
            "ImageBuilderInstanceProfile",
            roles=[self.instance_role.role_name],
            instance_profile_name=f"{config.org_name}-gvisor-image-builder",
        )

        # Security group for Image Builder instances
        self.security_group = ec2.SecurityGroup(
            self,
            "ImageBuilderSecurityGroup",
            vpc=vpc,
            description="Security group for gVisor Image Builder instances",
            allow_all_outbound=True,
        )

        # Component to install gVisor
        self.gvisor_component = imagebuilder.CfnComponent(
            self,
            "GvisorComponent",
            name=f"{config.org_name}-gvisor-install",
            platform="Linux",
            version="1.0.6",
            description="Install gVisor (runsc) runtime binaries for containerd",
            data=self._get_component_data(),
        )

        # Get EKS-optimized AL2023 AMI from SSM
        eks_ami_param = cdk.aws_ssm.StringParameter.value_for_string_parameter(
            self,
            "/aws/service/eks/optimized-ami/1.31/amazon-linux-2023/x86_64/standard/recommended/image_id",
        )

        # Image recipe based on EKS-optimized AL2023 AMI
        self.recipe = imagebuilder.CfnImageRecipe(
            self,
            "GvisorRecipe",
            name=f"{config.org_name}-al2023-gvisor",
            version="1.0.7",
            parent_image=eks_ami_param,
            components=[
                imagebuilder.CfnImageRecipe.ComponentConfigurationProperty(
                    component_arn=self.gvisor_component.attr_arn,
                )
            ],
            block_device_mappings=[
                imagebuilder.CfnImageRecipe.InstanceBlockDeviceMappingProperty(
                    device_name="/dev/xvda",
                    ebs=imagebuilder.CfnImageRecipe.EbsInstanceBlockDeviceSpecificationProperty(
                        volume_size=50,
                        volume_type="gp3",
                        delete_on_termination=True,
                    ),
                )
            ],
        )

        # Infrastructure configuration
        self.infra_config = imagebuilder.CfnInfrastructureConfiguration(
            self,
            "GvisorInfraConfig",
            name=f"{config.org_name}-gvisor-infra",
            instance_profile_name=self.instance_profile.instance_profile_name,
            instance_types=["t3.medium"],
            subnet_id=vpc.private_subnets[0].subnet_id,
            security_group_ids=[self.security_group.security_group_id],
            terminate_instance_on_failure=True,
        )
        self.infra_config.add_dependency(self.instance_profile)

        # Distribution configuration
        self.dist_config = imagebuilder.CfnDistributionConfiguration(
            self,
            "GvisorDistConfig",
            name=f"{config.org_name}-gvisor-dist",
            distributions=[
                imagebuilder.CfnDistributionConfiguration.DistributionProperty(
                    region=config.aws_region,
                    ami_distribution_configuration=imagebuilder.CfnDistributionConfiguration.AmiDistributionConfigurationProperty(
                        name=f"{config.org_name}-al2023-gvisor-{{{{imagebuilder:buildDate}}}}",
                        ami_tags={
                            "Name": f"{config.org_name}-al2023-gvisor",
                            "Environment": config.environment,
                            "ManagedBy": "ImageBuilder",
                            "Runtime": "gvisor",
                        },
                    ),
                )
            ],
        )

        # Image pipeline
        self.pipeline = imagebuilder.CfnImagePipeline(
            self,
            "GvisorPipeline",
            name=f"{config.org_name}-gvisor-pipeline",
            image_recipe_arn=self.recipe.attr_arn,
            infrastructure_configuration_arn=self.infra_config.attr_arn,
            distribution_configuration_arn=self.dist_config.attr_arn,
            status="ENABLED",
            image_tests_configuration=imagebuilder.CfnImagePipeline.ImageTestsConfigurationProperty(
                image_tests_enabled=True,
                timeout_minutes=60,
            ),
        )

        # Output the pipeline ARN for manual triggering
        CfnOutput(
            self,
            "GvisorPipelineArn",
            value=self.pipeline.attr_arn,
            description="gVisor AMI Image Builder Pipeline ARN",
            export_name=f"{config.org_name}-gvisor-pipeline-arn",
        )

    def _get_component_data(self) -> str:
        """Return the Image Builder component YAML for installing gVisor.

        Installs binaries and creates a drop-in config in /etc/containerd/config.d/
        which is automatically imported by the EKS AL2023 containerd config.
        The EC2NodeClass userData also configures the runtime via nodeadm as backup.
        """
        return """name: InstallGvisor
description: Install gVisor runtime binaries for containerd
schemaVersion: 1.0

phases:
  - name: build
    steps:
      - name: InstallGvisorBinaries
        action: ExecuteBash
        inputs:
          commands:
            - set -ex
            - ARCH=$(uname -m)
            - URL=https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}
            - cd /tmp
            - curl -fsSL -O ${URL}/runsc -O ${URL}/runsc.sha512 -O ${URL}/containerd-shim-runsc-v1 -O ${URL}/containerd-shim-runsc-v1.sha512
            - sha512sum -c runsc.sha512
            - sha512sum -c containerd-shim-runsc-v1.sha512
            - chmod a+rx runsc containerd-shim-runsc-v1
            - mv runsc containerd-shim-runsc-v1 /usr/bin/
            - /usr/bin/runsc --version

      - name: CreateContainerdDropin
        action: ExecuteBash
        inputs:
          commands:
            - set -ex
            - mkdir -p /etc/containerd/config.d
            - echo '[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]' > /etc/containerd/config.d/gvisor.toml
            - echo '  runtime_type = "io.containerd.runsc.v1"' >> /etc/containerd/config.d/gvisor.toml
            - cat /etc/containerd/config.d/gvisor.toml

  - name: validate
    steps:
      - name: ValidateGvisorBinaries
        action: ExecuteBash
        inputs:
          commands:
            - set -ex
            - test -x /usr/bin/runsc
            - test -x /usr/bin/containerd-shim-runsc-v1
            - test -f /etc/containerd/config.d/gvisor.toml
            - /usr/bin/runsc --version
            - echo "gVisor installation validated successfully"
"""

    @property
    def pipeline_arn(self) -> str:
        """Get the Image Builder pipeline ARN."""
        return self.pipeline.attr_arn
