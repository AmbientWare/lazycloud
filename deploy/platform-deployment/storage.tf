locals {
  # The Compose stack names these `lazycloud-objects`, `lazycloud-data` and
  # `lazycloud-source-packages`, which is fine against Garage and impossible
  # against S3, where a bucket name is global. Every one is prefixed here.
  object_buckets = toset(["objects", "data", "source-packages"])
}

resource "aws_s3_bucket" "objects" {
  for_each = local.object_buckets

  bucket = "${var.deployment}-${each.key}"

  # A deployment that cannot be destroyed cannot be proven to rebuild. Unlike
  # `prevent_destroy` this takes a variable, so it is a switch the owner flips
  # once these buckets hold something worth keeping.
  force_destroy = var.destroy_buckets_with_contents
}

resource "aws_s3_bucket_public_access_block" "objects" {
  for_each = aws_s3_bucket.objects

  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "objects" {
  for_each = aws_s3_bucket.objects

  bucket = each.value.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "objects" {
  for_each = aws_s3_bucket.objects

  bucket = each.value.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Where the deploy bundle lands: the digest-pinned Compose override CI writes and
# the instance reads at boot and on every redeploy.
resource "aws_s3_bucket" "deploy" {
  bucket        = "${var.deployment}-deploy"
  force_destroy = var.destroy_buckets_with_contents
}

resource "aws_s3_bucket_public_access_block" "deploy" {
  bucket                  = aws_s3_bucket.deploy.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "deploy" {
  bucket = aws_s3_bucket.deploy.id

  versioning_configuration {
    status = "Enabled"
  }
}
