# What only the reference platform (main before cutover) uses: its Redis,
# its PgBouncer-pooled database, its release distribution, its connected-AWS
# control principal and fleet connection role, and its S3 access-log
# pipeline. They stay so main's deployment can be restored after cutover.
# Delete this file, and the reference values in secrets.tf and identity.tf,
# when the user retires main's deployment (tasks/deploy.md, Decommission);
# the apply that follows destroys them.

variable "control_role_name" {
  description = <<-EOT
    Name of the reference platform's control principal. Customer trust
    policies name its ARN. Unset, it is `<deployment>-control-principal`.
  EOT
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.control_role_name == null || can(regex("^[A-Za-z0-9_+=,.@-]{1,64}$", var.control_role_name))
    error_message = "control_role_name must be a valid IAM role name."
  }
}

variable "control_plane_service_accounts" {
  description = "Service accounts of the reference platform's chart that hold the control plane's AWS identity."
  type = object({
    controlPlane = string
    scheduler    = string
  })
  default = { controlPlane = "control-plane", scheduler = "scheduler" }
}

variable "redis_node_type" {
  description = "ElastiCache node type of the reference platform's Redis."
  type        = string
  default     = "cache.t4g.micro"
}

variable "redis_engine_version" {
  description = "ElastiCache Redis engine version."
  type        = string
  default     = "7.1"
}

variable "database_pooler_max_connections" {
  description = "PgBouncer server connections of the reference platform's database branch."
  type        = number
  default     = 20
}

# The reference platform's database, schema and PgBouncer pool. The platform
# never connects to it.
resource "planetscale_postgres_branch" "control_plane" {
  organization  = var.planetscale_organization
  database      = var.deployment
  name          = "main"
  major_version = var.planetscale_major_version
  cluster_size  = var.planetscale_cluster_size
  region        = var.planetscale_region

  parameters = {
    pgconf = {
      max_connections = tostring(var.database_max_connections)
    }
    pgbouncer = {
      max_db_connections = tostring(var.database_pooler_max_connections)
    }
  }
}

resource "planetscale_postgres_branch_role" "control_plane" {
  organization    = var.planetscale_organization
  database        = planetscale_postgres_branch.control_plane.database
  branch          = planetscale_postgres_branch.control_plane.name
  inherited_roles = ["postgres"]
}

resource "random_password" "cache_service_token" {
  length  = 64
  special = false
}

locals {
  reference_platform_values = {
    LAZYCLOUD_DATABASE_URL = format(
      "postgresql+psycopg://%s:%s@%s:6432/%s",
      planetscale_postgres_branch_role.control_plane.username,
      planetscale_postgres_branch_role.control_plane.password,
      planetscale_postgres_branch_role.control_plane.access_host_url,
      planetscale_postgres_branch_role.control_plane.database_name,
    )
    LAZYCLOUD_DATABASE_DIRECT_URL = format(
      "postgresql+psycopg://%s:%s@%s:5432/%s",
      planetscale_postgres_branch_role.control_plane.username,
      planetscale_postgres_branch_role.control_plane.password,
      planetscale_postgres_branch_role.control_plane.access_host_url,
      planetscale_postgres_branch_role.control_plane.database_name,
    )
    LAZYCLOUD_CACHE_SERVICE_TOKEN                = random_password.cache_service_token.result
    LAZYCLOUD_PLATFORM_CAPACITY_AWS__EXTERNAL_ID = random_password.fleet_external_id.result
  }
}

# Leases, runtime state and published origins of the reference scheduler.
resource "aws_elasticache_subnet_group" "redis" {
  name       = "${var.deployment}-redis"
  subnet_ids = local.cluster_subnet_ids
}

resource "aws_security_group" "redis" {
  name        = "${var.deployment}-redis"
  description = "Redis: reachable only from inside this VPC."
  vpc_id      = local.cluster_vpc_id

  tags = { Name = "${var.deployment}-redis" }
}

resource "aws_vpc_security_group_ingress_rule" "redis" {
  security_group_id = aws_security_group.redis.id
  description       = "Cluster workloads."
  cidr_ipv4         = local.cluster_vpc_cidr
  from_port         = 6379
  to_port           = 6379
  ip_protocol       = "tcp"
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id = "${var.deployment}-redis"
  description          = "LazyCloud coordination: leases, runtime state, published origins."

  engine         = "redis"
  engine_version = var.redis_engine_version
  node_type      = var.redis_node_type
  port           = 6379

  num_cache_clusters         = 2
  automatic_failover_enabled = true
  multi_az_enabled           = true

  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.redis.id]

  transit_encryption_enabled = true
  at_rest_encryption_enabled = false
  apply_immediately          = true

  tags = { Name = "${var.deployment}-redis" }
}

# The control principal customer connections of the reference platform
# trust. Deleting it breaks those trusts for good: AWS rewrites a role ARN in
# a trust policy to the role's unique id.
locals {
  control_role_name = coalesce(var.control_role_name, "${var.deployment}-control-principal")
}

resource "aws_iam_role" "control_principal" {
  name        = local.control_role_name
  description = "LazyCloud platform control principal for connected AWS."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.control_plane.arn }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

data "aws_iam_policy_document" "control_principal" {
  statement {
    sid       = "AssumeCustomerConnectionRoles"
    actions   = ["sts:AssumeRole"]
    resources = ["${local.arn_prefix}:iam::*:role/*"]
  }

  statement {
    sid       = "InspectCapacityImages"
    actions   = ["ec2:DescribeImages"]
    resources = ["*"]
  }

  statement {
    sid       = "ShareOwnedCapacityImages"
    actions   = ["ec2:ModifyImageAttribute"]
    resources = ["${local.arn_prefix}:ec2:*::image/*"]

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/cloud-pool:managed-by"
      values   = ["control-plane"]
    }
  }
}

resource "aws_iam_role_policy" "control_principal" {
  name   = "connected-aws-control"
  role   = aws_iam_role.control_principal.name
  policy = data.aws_iam_policy_document.control_principal.json
}

# The reference fleet controller's Auto Scaling access to this account.
resource "aws_iam_role" "fleet_connection" {
  name        = "${var.deployment}-compute-connection"
  description = "Capacity management in the platform's own account."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.control_principal.arn }
        Action    = "sts:AssumeRole"
        Condition = {
          StringEquals = { "sts:ExternalId" = random_password.fleet_external_id.result }
        }
      },
      {
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.control_principal.arn }
        Action    = "sts:TagSession"
      },
    ]
  })
}

resource "random_password" "fleet_external_id" {
  length  = 48
  special = false
}

resource "aws_iam_role_policy" "fleet_connection" {
  name   = "managed-compute-control"
  role   = aws_iam_role.fleet_connection.name
  policy = file("${path.module}/connection-role-policy.json")
}

# releases.<apex>: agent archives, templates and node image catalogs the
# reference platform's hosts and customers download.
locals {
  release_hostname = "releases.${data.terraform_remote_state.cloudflare.outputs.records.apex}"
}

resource "aws_acm_certificate" "releases" {
  provider          = aws.certificate
  domain_name       = local.release_hostname
  validation_method = "DNS"

  lifecycle { create_before_destroy = true }
}

resource "cloudflare_dns_record" "release_certificate" {
  for_each = {
    for option in aws_acm_certificate.releases.domain_validation_options : option.domain_name => option
  }

  zone_id = data.terraform_remote_state.cloudflare.outputs.zone_id
  name    = each.value.resource_record_name
  type    = each.value.resource_record_type
  content = each.value.resource_record_value
  ttl     = 300
  proxied = false
}

resource "aws_acm_certificate_validation" "releases" {
  provider                = aws.certificate
  certificate_arn         = aws_acm_certificate.releases.arn
  validation_record_fqdns = [for record in cloudflare_dns_record.release_certificate : record.name]
}

resource "aws_cloudfront_origin_access_control" "releases" {
  name                              = "${var.deployment}-releases"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "releases" {
  enabled         = true
  is_ipv6_enabled = true
  aliases         = [local.release_hostname]
  comment         = "Immutable LazyCloud releases and the current host-image catalog."

  origin {
    domain_name              = aws_s3_bucket.storage["releases"].bucket_regional_domain_name
    origin_id                = "releases"
    origin_access_control_id = aws_cloudfront_origin_access_control.releases.id
  }

  default_cache_behavior {
    target_origin_id       = "releases"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    min_ttl                = 0
    default_ttl            = 3600
    max_ttl                = 31536000

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.releases.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

resource "aws_s3_bucket_policy" "releases" {
  bucket = aws_s3_bucket.storage["releases"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.storage["releases"].arn}/*"
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.releases.arn } }
    }]
  })
}

resource "cloudflare_dns_record" "releases" {
  zone_id = data.terraform_remote_state.cloudflare.outputs.zone_id
  name    = local.release_hostname
  type    = "CNAME"
  content = aws_cloudfront_distribution.releases.domain_name
  ttl     = 300
  proxied = false
}

# S3 server access logs of the objects bucket, which the reference fleet
# controller read from SQS to observe egress. The platform reads neither.
resource "aws_s3_bucket" "storage_access" {
  bucket = "${var.deployment}-storage-access-${data.aws_caller_identity.current.account_id}"
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_public_access_block" "storage_access" {
  bucket                  = aws_s3_bucket.storage_access.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "storage_access" {
  bucket = aws_s3_bucket.storage_access.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "storage_access" {
  bucket = aws_s3_bucket.storage_access.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "storage_access" {
  bucket = aws_s3_bucket.storage_access.id
  rule {
    id     = "expire-delivered-access-logs"
    status = "Enabled"
    filter { prefix = "access/" }
    expiration { days = 30 }
  }
}

resource "aws_s3_bucket_policy" "storage_access" {
  bucket = aws_s3_bucket.storage_access.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "logging.s3.amazonaws.com" }
      Action    = "s3:PutObject"
      Resource  = "${aws_s3_bucket.storage_access.arn}/access/*"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike      = { "aws:SourceArn" = [aws_s3_bucket.storage["objects"].arn, local.workspace_bucket_arn] }
      }
    }]
  })
}

resource "aws_s3_bucket_logging" "objects" {
  bucket        = aws_s3_bucket.storage["objects"].id
  target_bucket = aws_s3_bucket.storage_access.id
  target_prefix = "access/"
  depends_on    = [aws_s3_bucket_policy.storage_access, aws_s3_bucket_notification.storage_access]
}

resource "aws_sqs_queue" "storage_access_dead_letters" {
  name                      = "${var.deployment}-storage-access-dead-letters"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "storage_access" {
  name                       = "${var.deployment}-storage-access"
  message_retention_seconds  = 1209600
  visibility_timeout_seconds = 120
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.storage_access_dead_letters.arn
    maxReceiveCount     = 10
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "storage_access" {
  queue_url = aws_sqs_queue.storage_access_dead_letters.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.storage_access.arn]
  })
}

resource "aws_sqs_queue_policy" "storage_access" {
  queue_url = aws_sqs_queue.storage_access.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.storage_access.arn
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnEquals    = { "aws:SourceArn" = aws_s3_bucket.storage_access.arn }
      }
    }]
  })
}

resource "aws_s3_bucket_notification" "storage_access" {
  bucket = aws_s3_bucket.storage_access.id
  queue {
    queue_arn     = aws_sqs_queue.storage_access.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "access/"
  }
  depends_on = [aws_sqs_queue_policy.storage_access]
}

# The objects bucket's CORS rule. The server sets the same rule at start
# (storage.AllowBrowserAccess), which owns it once this file goes.
resource "aws_s3_bucket_cors_configuration" "objects" {
  bucket = aws_s3_bucket.storage["objects"].id
  cors_rule {
    allowed_origins = ["https://${data.terraform_remote_state.cloudflare.outputs.records.apex}"]
    allowed_methods = ["GET", "HEAD", "PUT"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "x-amz-checksum-sha256"]
    max_age_seconds = 3600
  }
}

data "aws_iam_policy_document" "reference_control_plane" {
  statement {
    sid       = "ReadStorageAccessLogs"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.storage_access.arn}/access/*"]
  }

  statement {
    sid       = "ConsumeStorageAccessDeliveries"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.storage_access.arn]
  }

  statement {
    sid       = "PullControlPlaneImages"
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = local.ecr_repository_arns
  }

  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_secretsmanager_secret.platform.arn, aws_secretsmanager_secret.operator.arn]
  }
}
