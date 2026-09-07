locals {
  cloudflare_account_id   = nonsensitive(jsondecode(data.terraform_remote_state.cloudflare.outputs.tunnel_credentials).AccountTag)
  workspace_bucket_prefix = "${var.deployment}-workspace"
}

resource "cloudflare_r2_bucket" "storage" {
  for_each = toset(["objects", "deploy"])

  account_id    = local.cloudflare_account_id
  name          = "${var.deployment}-${each.key}"
  location      = "enam"
  storage_class = "Standard"

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_r2_bucket_lifecycle" "storage" {
  for_each = cloudflare_r2_bucket.storage

  account_id  = local.cloudflare_account_id
  bucket_name = each.value.name
  rules = [{
    id         = "abort-incomplete-uploads"
    enabled    = true
    conditions = { prefix = "" }
    abort_multipart_uploads_transition = {
      condition = { type = "Age", max_age = 86400 }
    }
  }]
}

resource "cloudflare_r2_managed_domain" "storage" {
  for_each = cloudflare_r2_bucket.storage

  account_id  = local.cloudflare_account_id
  bucket_name = each.value.name
  enabled     = false
}

resource "cloudflare_r2_bucket_cors" "objects" {
  account_id  = local.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.storage["objects"].name
  rules = [{
    id = "dashboard-transfers"
    allowed = {
      origins = ["https://${data.terraform_remote_state.cloudflare.outputs.records.apex}"]
      methods = ["GET", "HEAD", "PUT"]
      headers = ["*"]
    }
    expose_headers  = ["ETag", "x-amz-checksum-sha256"]
    max_age_seconds = 3600
  }]
}
