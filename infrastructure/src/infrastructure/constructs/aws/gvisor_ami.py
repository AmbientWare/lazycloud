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
            version="1.0.0",
            description="Install gVisor (runsc) runtime for containerd",
            data=self._get_component_data(),
        )

        # Image recipe based on AL2023 EKS-optimized AMI
        self.recipe = imagebuilder.CfnImageRecipe(
            self,
            "GvisorRecipe",
            name=f"{config.org_name}-al2023-gvisor",
            version="1.0.0",
            parent_image=f"arn:aws:imagebuilder:{config.aws_region}:aws:image/amazon-linux-2023-x86/x.x.x",
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
        """Return the Image Builder component YAML for installing gVisor."""
        return """name: InstallGvisor
description: Install gVisor runtime for containerd
schemaVersion: 1.0

phases:
  - name: build
    steps:
      - name: InstallGvisorBinaries
        action: ExecuteBash
        inputs:
          commands:
            - |
              set -ex
              ARCH=$(uname -m)
              URL="https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}"

              cd /tmp
              curl -fsSL -O "${URL}/runsc" \
                         -O "${URL}/runsc.sha512" \
                         -O "${URL}/containerd-shim-runsc-v1" \
                         -O "${URL}/containerd-shim-runsc-v1.sha512"

              sha512sum -c runsc.sha512
              sha512sum -c containerd-shim-runsc-v1.sha512

              chmod a+rx runsc containerd-shim-runsc-v1
              mv runsc containerd-shim-runsc-v1 /usr/local/bin/

              # Verify installation
              /usr/local/bin/runsc --version

      - name: ConfigureContainerd
        action: ExecuteBash
        inputs:
          commands:
            - |
              set -ex
              mkdir -p /etc/containerd/config.d

              cat > /etc/containerd/config.d/gvisor.toml << 'EOF'
              [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
                runtime_type = "io.containerd.runsc.v1"
              EOF

              echo "gVisor containerd configuration created"

  - name: validate
    steps:
      - name: ValidateGvisor
        action: ExecuteBash
        inputs:
          commands:
            - |
              set -ex
              # Verify binaries exist
              test -x /usr/local/bin/runsc
              test -x /usr/local/bin/containerd-shim-runsc-v1

              # Verify config exists
              test -f /etc/containerd/config.d/gvisor.toml

              # Verify runsc works
              /usr/local/bin/runsc --version

              echo "gVisor validation successful"
"""

    @property
    def pipeline_arn(self) -> str:
        """Get the Image Builder pipeline ARN."""
        return self.pipeline.attr_arn
