# objects holds sources, artifacts and build contexts; deploy holds the
# chart values; the layer buckets below hold converted images. Workspace buckets (<prefix>-<workspace id>) hold volumes and
# disks; the server creates them, so they stay outside Terraform.
locals {
  workspace_bucket_prefix = "${var.deployment}-workspace"
  workspace_bucket_arn    = "${local.arn_prefix}:s3:::${local.workspace_bucket_prefix}-*"
}

resource "aws_s3_bucket" "storage" {
  for_each = toset(["objects", "deploy"])
  bucket   = "${var.deployment}-${each.key}-${local.account_id}"
}

resource "aws_s3_bucket_public_access_block" "storage" {
  for_each                = aws_s3_bucket.storage
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "storage" {
  for_each = aws_s3_bucket.storage
  bucket   = each.value.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# What a host's grant assumes. The server narrows each session to one
# workspace bucket's volumes/ and disks/ with a session policy
# (storage.hostPolicy); this role bounds every session to workspace buckets.
resource "aws_iam_role" "workspace_storage" {
  name = "${var.deployment}-workspace-storage"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { AWS = aws_iam_role.control_plane.arn }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "workspace_storage" {
  name = "workspace-objects"
  role = aws_iam_role.workspace_storage.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListBucket", "s3:ListBucketMultipartUploads"], Resource = local.workspace_bucket_arn },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${local.workspace_bucket_arn}/*"
      },
    ]
  })
}

# Converted image layers: layers/<id>/index and layers/<id>/data in the
# bucket of var.region, which S3 replication copies to one bucket in every
# other fleet region so hosts read frames in their own region. Pairs are
# written once and deleted by the layer sweep; replication carries the
# delete markers, and noncurrent versions go a day later in every copy.
locals {
  layer_replica_regions = setsubtract(local.fleet_regions, [var.region])
  layer_bucket_arn      = aws_s3_bucket.layers[var.region].arn
  layer_replica_arns    = [for region in local.layer_replica_regions : aws_s3_bucket.layers[region].arn]
}

resource "aws_s3_bucket" "layers" {
  for_each = local.fleet_regions
  region   = each.key
  bucket   = each.key == var.region ? "${var.deployment}-layers-${local.account_id}" : "${var.deployment}-layers-${each.key}-${local.account_id}"
}

resource "aws_s3_bucket_public_access_block" "layers" {
  for_each                = aws_s3_bucket.layers
  region                  = each.key
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "layers" {
  for_each = aws_s3_bucket.layers
  region   = each.key
  bucket   = each.value.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "layers" {
  for_each = aws_s3_bucket.layers
  region   = each.key
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "layers" {
  for_each   = aws_s3_bucket.layers
  region     = each.key
  bucket     = each.value.id
  depends_on = [aws_s3_bucket_versioning.layers]
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
  rule {
    id     = "expire-deleted-layers"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
    expiration {
      expired_object_delete_marker = true
    }
  }
}

# Replication Time Control copies nearly every object within 15 minutes;
# until images confirms a copy holds a layer, hosts read it from var.region.
resource "aws_s3_bucket_replication_configuration" "layers" {
  bucket     = aws_s3_bucket.layers[var.region].id
  role       = aws_iam_role.layer_replication.arn
  depends_on = [aws_s3_bucket_versioning.layers]

  dynamic "rule" {
    for_each = { for priority, region in sort(tolist(local.layer_replica_regions)) : region => priority }
    content {
      id       = rule.key
      priority = rule.value
      status   = "Enabled"
      filter {}
      delete_marker_replication {
        status = "Enabled"
      }
      destination {
        bucket        = aws_s3_bucket.layers[rule.key].arn
        storage_class = "STANDARD"
        replication_time {
          status = "Enabled"
          time {
            minutes = 15
          }
        }
        metrics {
          status = "Enabled"
          event_threshold {
            minutes = 15
          }
        }
      }
    }
  }
}

resource "aws_iam_role" "layer_replication" {
  name = "${var.deployment}-layer-replication"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "s3.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "layer_replication" {
  name = "replicate-layers"
  role = aws_iam_role.layer_replication.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetReplicationConfiguration", "s3:ListBucket"], Resource = local.layer_bucket_arn },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObjectVersionForReplication", "s3:GetObjectVersionAcl", "s3:GetObjectVersionTagging"]
        Resource = "${local.layer_bucket_arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ReplicateObject", "s3:ReplicateDelete", "s3:ReplicateTags"]
        Resource = [for arn in local.layer_replica_arns : "${arn}/*"]
      },
    ]
  })
}
