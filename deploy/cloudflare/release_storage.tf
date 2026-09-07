resource "cloudflare_r2_bucket" "releases" {
  account_id    = var.account_id
  name          = "lazycloud-${var.environment}-releases"
  location      = "enam"
  storage_class = "Standard"

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_r2_bucket_lifecycle" "releases" {
  account_id  = var.account_id
  bucket_name = cloudflare_r2_bucket.releases.name
  rules = [{
    id         = "abort-incomplete-uploads"
    enabled    = true
    conditions = { prefix = "" }
    abort_multipart_uploads_transition = {
      condition = { type = "Age", max_age = 86400 }
    }
  }]
}

resource "cloudflare_r2_managed_domain" "releases" {
  account_id  = var.account_id
  bucket_name = cloudflare_r2_bucket.releases.name
  enabled     = false
}

resource "cloudflare_r2_custom_domain" "releases" {
  account_id  = var.account_id
  bucket_name = cloudflare_r2_bucket.releases.name
  domain      = "releases.${var.public_hostname}"
  zone_id     = var.zone_id
  enabled     = true
  min_tls     = "1.2"
}
