locals {
  # The account identifier is public; the other fields in this document are secrets.
  cloudflare_account_id = nonsensitive(jsondecode(data.terraform_remote_state.cloudflare.outputs.tunnel_credentials).AccountTag)
}

resource "cloudflare_r2_bucket" "image_archives" {
  account_id    = local.cloudflare_account_id
  name          = "${var.deployment}-image-archives"
  location      = "enam"
  storage_class = "Standard"

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_r2_bucket_lifecycle" "image_archives" {
  account_id  = local.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.image_archives.name
  rules = [{
    id         = "abort-incomplete-uploads"
    enabled    = true
    conditions = { prefix = "" }
    abort_multipart_uploads_transition = {
      condition = { type = "Age", max_age = 86400 }
    }
  }]
}

resource "cloudflare_r2_managed_domain" "image_archives" {
  account_id  = local.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.image_archives.name
  enabled     = false
}
