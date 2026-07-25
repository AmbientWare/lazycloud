terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region
}

locals {
  name = var.service_name
  tags = merge(var.tags, {
    Service = var.service_name
  })
}

resource "aws_vpc" "network" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = "${local.name}-vpc" })
}

resource "aws_subnet" "public" {
  count                   = length(var.public_subnet_cidrs)
  vpc_id                  = aws_vpc.network.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = true
  tags                    = merge(local.tags, { Name = "${local.name}-public-${count.index}" })
}

resource "aws_subnet" "private" {
  count             = length(var.private_subnet_cidrs)
  vpc_id            = aws_vpc.network.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]
  tags              = merge(local.tags, { Name = "${local.name}-private-${count.index}" })
}

resource "aws_internet_gateway" "gateway" {
  vpc_id = aws_vpc.network.id
  tags   = merge(local.tags, { Name = "${local.name}-igw" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.network.id
  tags   = merge(local.tags, { Name = "${local.name}-public" })

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.gateway.id
  }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = merge(local.tags, { Name = "${local.name}-nat" })
}

resource "aws_nat_gateway" "nat" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = merge(local.tags, { Name = "${local.name}-nat" })

  depends_on = [aws_internet_gateway.gateway]
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.network.id
  tags   = merge(local.tags, { Name = "${local.name}-private" })

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat.id
  }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_security_group" "control_plane" {
  name        = "${local.name}-control-plane"
  description = "Control-plane ingress and cluster traffic"
  vpc_id      = aws_vpc.network.id
  tags        = local.tags

  ingress {
    description = "HTTP API"
    from_port   = var.control_plane_port
    to_port     = var.control_plane_port
    protocol    = "tcp"
    cidr_blocks = var.allowed_ingress_cidrs
  }

  ingress {
    description = "Kubernetes API"
    from_port   = 6443
    to_port     = 6443
    protocol    = "tcp"
    cidr_blocks = var.allowed_ingress_cidrs
  }

  ingress {
    description = "SSH administration and Terraform chart install"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = var.ssh_ingress_cidrs
  }

  ingress {
    description = "Cluster node-to-node traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
  }

  egress {
    description = "Outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_iam_role" "node" {
  name = "${local.name}-node"
  tags = local.tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "node" {
  name = "${local.name}-node"
  role = aws_iam_role.node.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:PutParameter",
          "ssm:DescribeParameters",
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket",
          "ecr:GetAuthorizationToken"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "node" {
  name = "${local.name}-node"
  role = aws_iam_role.node.name
}

resource "aws_s3_bucket" "objects" {
  bucket = var.object_bucket_name
  tags   = local.tags
}

resource "aws_s3_bucket" "images" {
  bucket = var.image_bucket_name
  tags   = local.tags
}

resource "aws_iam_user" "object_store" {
  name = "${local.name}-object-store"
  tags = local.tags
}

resource "aws_iam_access_key" "object_store" {
  user = aws_iam_user.object_store.name
}

resource "aws_iam_user_policy" "object_store" {
  name = "${local.name}-object-store"
  user = aws_iam_user.object_store.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:AbortMultipartUpload",
          "s3:ListBucketMultipartUploads"
        ]
        Resource = [
          aws_s3_bucket.objects.arn,
          "${aws_s3_bucket.objects.arn}/*",
          aws_s3_bucket.images.arn,
          "${aws_s3_bucket.images.arn}/*"
        ]
      }
    ]
  })
}

resource "aws_db_subnet_group" "database" {
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.private[*].id
  tags       = local.tags
}

resource "aws_security_group" "database" {
  name        = "${local.name}-database"
  description = "Database access from cluster nodes"
  vpc_id      = aws_vpc.network.id
  tags        = local.tags

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.control_plane.id]
  }
}

resource "random_password" "database" {
  length  = 24
  special = false
}

resource "random_password" "juicefs_gateway_access_key" {
  length  = 20
  special = false
}

resource "random_password" "juicefs_gateway_secret_key" {
  length  = 40
  special = false
}

resource "random_password" "cache_service_token" {
  length  = 48
  special = false
}

resource "aws_db_instance" "database" {
  identifier             = "${local.name}-postgres"
  allocated_storage      = var.database_storage_gb
  engine                 = "postgres"
  engine_version         = var.postgres_version
  instance_class         = var.database_instance_class
  db_name                = var.database_name
  username               = var.database_username
  password               = random_password.database.result
  db_subnet_group_name   = aws_db_subnet_group.database.name
  vpc_security_group_ids = [aws_security_group.database.id]
  skip_final_snapshot    = var.skip_final_snapshot
  tags                   = local.tags
}

resource "aws_instance" "control_plane" {
  ami                         = var.node_ami
  instance_type               = var.control_plane_instance_type
  key_name                    = var.ssh_key_name
  subnet_id                   = aws_subnet.public[0].id
  vpc_security_group_ids      = [aws_security_group.control_plane.id]
  iam_instance_profile        = aws_iam_instance_profile.node.name
  associate_public_ip_address = true
  user_data = templatefile("${path.module}/templates/k3s-control-plane.sh.tftpl", {
    control_plane_port   = var.control_plane_port
    node_token_parameter = "/${local.name}/k3s/node-token"
  })
  tags = merge(local.tags, { Name = "${local.name}-control-plane" })
}

resource "aws_instance" "worker" {
  count                  = var.worker_count
  ami                    = var.node_ami
  instance_type          = var.worker_instance_type
  key_name               = var.ssh_key_name
  subnet_id              = aws_subnet.private[count.index % length(aws_subnet.private)].id
  vpc_security_group_ids = [aws_security_group.control_plane.id]
  iam_instance_profile   = aws_iam_instance_profile.node.name
  user_data = templatefile("${path.module}/templates/k3s-worker.sh.tftpl", {
    server_url           = "https://${aws_instance.control_plane.private_ip}:6443"
    node_token_parameter = "/${local.name}/k3s/node-token"
  })
  tags = merge(local.tags, { Name = "${local.name}-worker-${count.index}" })
}

locals {
  chart_directory = "${path.module}/../../charts/lazycloud"
  chart_files     = sort(tolist(fileset(local.chart_directory, "**")))
  chart_content_sha256 = sha256(join("\n", [
    for relative_path in local.chart_files :
    "${relative_path}:${filesha256("${local.chart_directory}/${relative_path}")}"
  ]))
  chart_namespace            = "lazycloud-system"
  chart_external_secret_name = "lazycloud-external-services"
  chart_redis_url            = "redis://lazycloud-redis:6379/0"
  chart_juicefs_metadata_url = "redis://lazycloud-redis:6379/1"
  chart_external_secret_data = {
    LAZYCLOUD_DATABASE_URL                   = "postgresql+psycopg://${urlencode(var.database_username)}:${urlencode(random_password.database.result)}@${aws_db_instance.database.address}:5432/${urlencode(var.database_name)}"
    LAZYCLOUD_REDIS_URL                      = local.chart_redis_url
    token                                    = random_password.cache_service_token.result
    LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID     = aws_iam_access_key.object_store.id
    LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY = aws_iam_access_key.object_store.secret
    LAZYCLOUD_JUICEFS_GATEWAY_ACCESS_KEY_ID  = random_password.juicefs_gateway_access_key.result
    LAZYCLOUD_JUICEFS_GATEWAY_SECRET_ACCESS_KEY = (
      random_password.juicefs_gateway_secret_key.result
    )
  }
  chart_external_secret_manifest = {
    apiVersion = "v1"
    kind       = "Secret"
    metadata = {
      name      = local.chart_external_secret_name
      namespace = local.chart_namespace
    }
    type       = "Opaque"
    stringData = local.chart_external_secret_data
  }
  chart_external_secret_revision = sha256(jsonencode(local.chart_external_secret_data))
  chart_install_values = {
    image = {
      repository = var.api_image
      tag        = var.image_tag
      pullPolicy = var.image_pull_policy
    }
    config = {
      create                 = false
      existingSecret         = local.chart_external_secret_name
      externalSecretRevision = local.chart_external_secret_revision
    }
    controlPlane = {
      publicHttpUrl = var.control_plane_public_http_url
    }
    database = {
      secretRef = {
        name = local.chart_external_secret_name
        key  = "LAZYCLOUD_DATABASE_URL"
      }
    }
    databaseBootstrap = {
      image = {
        repository = var.database_bootstrap_image
        tag        = var.image_tag
        pullPolicy = var.image_pull_policy
      }
    }
    administratorBootstrap = {
      image = {
        repository = var.worker_bootstrap_image
        tag        = var.image_tag
        pullPolicy = var.image_pull_policy
      }
    }
    redis = {
      secretRef = {
        name = local.chart_external_secret_name
        key  = "LAZYCLOUD_REDIS_URL"
      }
    }
    redisStateful = {
      enabled = true
    }
    scheduler = {
      image = {
        repository = var.scheduler_image
        tag        = var.image_tag
        pullPolicy = var.image_pull_policy
      }
    }
    containerWorkers = {
      image = {
        repository = var.container_worker_image
        tag        = var.image_tag
        pullPolicy = var.image_pull_policy
      }
      workerTokenBootstrap = {
        image = {
          repository = var.worker_bootstrap_image
          tag        = var.image_tag
          pullPolicy = var.image_pull_policy
        }
      }
    }
    juicefs = {
      metadataUrl = local.chart_juicefs_metadata_url
      gateway = {
        credentialsSecretRef = {
          name               = local.chart_external_secret_name
          accessKeyIdKey     = "LAZYCLOUD_JUICEFS_GATEWAY_ACCESS_KEY_ID"
          secretAccessKeyKey = "LAZYCLOUD_JUICEFS_GATEWAY_SECRET_ACCESS_KEY"
        }
        image = {
          repository = var.storage_gateway_image
          tag        = var.image_tag
          pullPolicy = var.image_pull_policy
        }
      }
    }
    cache = {
      createSecret   = false
      existingSecret = local.chart_external_secret_name
      image = {
        repository = var.cache_image
        tag        = var.image_tag
        pullPolicy = var.image_pull_policy
      }
    }
    service = {
      type = "LoadBalancer"
      port = var.control_plane_port
    }
    postgresql = {
      enabled = false
    }
    objectStore = {
      enabled             = false
      endpointUrl         = "https://s3.${var.region}.amazonaws.com"
      bucket              = aws_s3_bucket.objects.bucket
      defaultBucket       = aws_s3_bucket.objects.bucket
      dataBucket          = aws_s3_bucket.objects.bucket
      sourcePackageBucket = aws_s3_bucket.objects.bucket
      regionName          = var.region
      forcePathStyle      = false
      credentialsSecretRef = {
        name               = local.chart_external_secret_name
        accessKeyIdKey     = "LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID"
        secretAccessKeyKey = "LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY"
      }
    }
    imageBuild = {
      archive = {
        bucket = aws_s3_bucket.images.bucket
      }
    }
    ingress = {
      enabled   = var.ingress_enabled
      className = "nginx"
      hosts = [{
        host = var.hostname
        paths = [{
          path     = "/"
          pathType = "Prefix"
        }]
      }]
      tls = [{
        hosts      = [var.hostname]
        secretName = "lazycloud-control-plane-tls"
      }]
    }
    monitoring = {
      enabled = var.monitoring_enabled
    }
  }
  chart_install_contract_sha256 = sha256(jsonencode({
    chart_sha256 = local.chart_content_sha256
    secret       = local.chart_external_secret_manifest
    values       = local.chart_install_values
  }))
}

resource "terraform_data" "chart_install" {
  count = var.install_chart ? 1 : 0

  triggers_replace = [
    aws_instance.control_plane.id,
    aws_db_instance.database.id,
    aws_s3_bucket.objects.id,
    aws_s3_bucket.images.id,
    local.chart_install_contract_sha256,
  ]

  lifecycle {
    precondition {
      condition     = var.control_plane_public_http_url != ""
      error_message = "control_plane_public_http_url must be set when install_chart is true."
    }
  }

  connection {
    type        = "ssh"
    host        = aws_instance.control_plane.public_ip
    user        = var.ssh_user
    private_key = file(var.ssh_private_key_path)
    timeout     = "10m"
  }

  provisioner "file" {
    source      = local.chart_directory
    destination = "/tmp/lazycloud-chart"
  }

  provisioner "file" {
    content     = yamlencode(local.chart_install_values)
    destination = "/tmp/lazycloud-values.yaml"
  }

  provisioner "remote-exec" {
    inline = [
      "install -d -m 0700 /tmp/lazycloud-install"
    ]
  }

  provisioner "file" {
    content     = yamlencode(local.chart_external_secret_manifest)
    destination = "/tmp/lazycloud-install/external-services-secret.yaml"
  }

  provisioner "remote-exec" {
    inline = [
      <<-EOT
      set -eu
      secret_file=/tmp/lazycloud-install/external-services-secret.yaml
      values_file=/tmp/lazycloud-values.yaml
      cleanup() {
        rm -f "$secret_file"
        rm -f "$values_file"
        rmdir /tmp/lazycloud-install 2>/dev/null || true
      }
      trap cleanup EXIT
      chmod 0600 "$secret_file"
      test "$(stat -c '%a' "$secret_file")" = "600"
      until sudo test -f /etc/rancher/k3s/k3s.yaml; do sleep 5; done
      if ! command -v helm >/dev/null 2>&1; then
        curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
      fi
      helm lint /tmp/lazycloud-chart --values "$values_file"
      helm template lazycloud /tmp/lazycloud-chart \
        --namespace ${local.chart_namespace} --values "$values_file" >/dev/null
      sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml \
        kubectl create namespace ${local.chart_namespace} --dry-run=client -o yaml \
        | sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl apply -f - >/dev/null
      sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl apply -f "$secret_file" >/dev/null
      rm -f "$secret_file"
      rmdir /tmp/lazycloud-install 2>/dev/null || true
      sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml \
        helm upgrade --install lazycloud /tmp/lazycloud-chart \
        --namespace ${local.chart_namespace} --create-namespace \
        --values "$values_file" --wait --timeout 10m
      rm -f "$values_file"
      trap - EXIT
      EOT
    ]
  }

  depends_on = [
    aws_instance.worker,
    aws_db_instance.database,
    aws_iam_user_policy.object_store
  ]
}
