data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

# One instance, deliberately. The API is replica-safe and advertises a Tailscale
# Service, so a second host is a second advertiser rather than a load balancer,
# but nothing is gained by running one before there is traffic to justify it.
# Redis lives on this host and is the thing that has to move first when a second
# one is wanted.
resource "aws_instance" "control_plane" {
  ami                    = data.aws_ssm_parameter.al2023_arm64.value
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
  control_plane_user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail

    dnf install -y docker
    systemctl enable --now docker

    install -d -m 0755 /usr/local/lib/docker/cli-plugins
    ARCH="$(uname -m)"
    curl -fsSL \
      "https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux_$${ARCH}" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose
    chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose

    install -d -m 0700 /opt/lazycloud
    cat >/usr/local/bin/lazycloud-deploy <<'DEPLOY'
    #!/bin/bash
    # Pull the current deployment bundle and converge the stack onto it. Run at
    # boot and again by SSM on every release; it is the only thing that changes
    # what this host runs.
    set -euo pipefail
    BUNDLE="s3://${aws_s3_bucket.deploy.id}/current"
    cd /opt/lazycloud
    aws s3 cp "$BUNDLE/compose.yaml" compose.yaml
    aws s3 cp "$BUNDLE/compose.deploy.yaml" compose.deploy.yaml
    aws s3 cp "$BUNDLE/deployment.env" deployment.env
    aws ecr get-login-password --region ${var.region} \
      | docker login --username AWS --password-stdin \
        ${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com
    docker compose -f compose.yaml -f compose.deploy.yaml --env-file deployment.env pull
    docker compose -f compose.yaml -f compose.deploy.yaml --env-file deployment.env up -d --remove-orphans
    DEPLOY
    chmod 0755 /usr/local/bin/lazycloud-deploy

    # Absent on a first apply: Terraform creates the bucket, CI writes the first
    # bundle into it. Boot should not fail because the release has not happened.
    if aws s3 ls "s3://${aws_s3_bucket.deploy.id}/current/compose.yaml" >/dev/null 2>&1; then
      /usr/local/bin/lazycloud-deploy
    fi
  EOT
}
