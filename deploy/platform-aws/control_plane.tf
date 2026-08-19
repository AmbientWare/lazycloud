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
  # Secrets are rendered on the host from Secrets Manager, never carried in the
  # bundle. The bundle is a build artifact that lands in a versioned bucket and
  # is read by anything with s3:GetObject on it; a credential in there outlives
  # every rotation and shows up in access logs.
  control_plane_user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail

    dnf install -y docker jq
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
    # Converge this host onto the current bundle. Run at boot and again by SSM on
    # every release; nothing else changes what runs here.
    set -euo pipefail
    umask 077
    cd /opt/lazycloud

    aws s3 cp "s3://${aws_s3_bucket.deploy.id}/current/compose.yaml" compose.yaml
    aws s3 cp "s3://${aws_s3_bucket.deploy.id}/current/compose.deploy.yaml" compose.deploy.yaml
    aws s3 cp "s3://${aws_s3_bucket.deploy.id}/current/images.env" images.env
    aws s3 cp "s3://${aws_s3_bucket.deploy.id}/current/runtime.env" runtime.env
    aws s3 cp "s3://${aws_s3_bucket.deploy.id}/current/services" services

    # One Secrets Manager read per secret, written to a mode-0600 file the compose
    # invocation reads and nothing else does.
    : >secrets.env
    chmod 0600 secrets.env
    while IFS='=' read -r variable secret; do
      [ -n "$variable" ] || continue
      value="$(aws secretsmanager get-secret-value \
        --secret-id "$secret" --query SecretString --output text)"
      printf '%s=%s\n' "$variable" "$value" >>secrets.env
    done </opt/lazycloud/secret-map

    aws ecr get-login-password --region ${var.region} \
      | docker login --username AWS --password-stdin \
        ${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com

    COMPOSE=(docker compose -f compose.yaml -f compose.deploy.yaml
      --env-file images.env --env-file runtime.env --env-file secrets.env
      --profile public-ingress)
    # shellcheck disable=SC2046
    "$${COMPOSE[@]}" pull $(cat services)
    # shellcheck disable=SC2046
    "$${COMPOSE[@]}" up -d --remove-orphans $(cat services)
    DEPLOY
    chmod 0755 /usr/local/bin/lazycloud-deploy

    cat >/opt/lazycloud/secret-map <<'SECRETS'
    ${indent(4, join("\n", [for variable, secret in local.secret_environment : "${variable}=${secret}"]))}
    SECRETS
    chmod 0600 /opt/lazycloud/secret-map

    # Absent on a first apply: Terraform creates the bucket, the release writes the
    # first bundle into it. Boot must not fail because that has not happened yet.
    if aws s3 ls "s3://${aws_s3_bucket.deploy.id}/current/compose.yaml" >/dev/null 2>&1; then
      /usr/local/bin/lazycloud-deploy
    fi
  EOT
}
