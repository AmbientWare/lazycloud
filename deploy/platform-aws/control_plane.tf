# x86_64, matching everything else. The release agent is published amd64 only and
# the node AMIs are baked amd64, so a Graviton control plane would be the single
# arm64 surface in the system and the only image that has to cross-build.
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# One instance, deliberately. The API is replica-safe and advertises a Tailscale
# Service, so a second host is a second advertiser rather than a load balancer,
# but nothing is gained by running one before there is traffic to justify it.
# Redis lives on this host and is the thing that has to move first when a second
# one is wanted.
resource "aws_instance" "control_plane" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.control_plane_instance_type
  subnet_id              = aws_subnet.control_plane.id
  vpc_security_group_ids = [aws_security_group.control_plane.id]
  iam_instance_profile   = aws_iam_instance_profile.control_plane.name

  user_data                   = local.control_plane_user_data
  user_data_replace_on_change = false

  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_size           = var.control_plane_root_volume_gib
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = false
  }

  tags = { Name = "${var.deployment}-control-plane" }

  lifecycle {
    # The AMI parameter moves whenever Amazon publishes a new AL2023 image, and
    # the stack is redeployed by pulling images rather than by replacing the host.
    ignore_changes = [ami]
  }
}

# A stable address the tunnel and the tailnet device can be rebuilt against
# without every consumer of the old one needing to be found.
resource "aws_eip" "control_plane" {
  instance = aws_instance.control_plane.id
  domain   = "vpc"

  tags = { Name = "${var.deployment}-control-plane" }
}

locals {
  # Deliberately thin. User data runs once, at first boot, so anything written
  # here can only be corrected by replacing the machine — and every defect so far
  # has been in the converge logic, not in this. That logic now ships in the
  # bundle, so a fix reaches the host the way an image does.
  control_plane_user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail

    dnf install -y docker
    systemctl enable --now docker

    install -d -m 0755 /usr/local/lib/docker/cli-plugins
    ARCH="$(uname -m)"
    curl -fsSL \
      "https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-$${ARCH}" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose
    chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose

    install -d -m 0700 /opt/lazycloud

    cat >/opt/lazycloud/secret-map <<'SECRETS'
    ${join("\n", [for variable, secret in local.secret_environment : "${variable}=${secret}"])}
    SECRETS
    chmod 0600 /opt/lazycloud/secret-map

    cat >/usr/local/bin/lazycloud-deploy <<'DEPLOY'
    #!/bin/bash
    # Fetch the current converge script and run it. The logic lives in the
    # bundle; this only knows where to find it.
    set -euo pipefail
    export LAZYCLOUD_BUNDLE_URI="s3://${aws_s3_bucket.deploy.id}/current"
    export LAZYCLOUD_REGION="${var.region}"
    export LAZYCLOUD_REGISTRY="${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
    aws s3 cp "$LAZYCLOUD_BUNDLE_URI/converge.sh" /opt/lazycloud/converge.sh
    chmod 0755 /opt/lazycloud/converge.sh
    exec /opt/lazycloud/converge.sh
    DEPLOY
    chmod 0755 /usr/local/bin/lazycloud-deploy

    # Absent on a first apply: Terraform creates the bucket, the release writes
    # the first bundle. Boot must not fail because that has not happened yet.
    if aws s3 ls "s3://${aws_s3_bucket.deploy.id}/current/converge.sh" >/dev/null 2>&1; then
      /usr/local/bin/lazycloud-deploy
    fi
  EOT
}
