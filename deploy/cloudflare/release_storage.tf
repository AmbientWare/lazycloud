removed {
  from = cloudflare_r2_bucket.releases
  lifecycle { destroy = false }
}

removed {
  from = cloudflare_r2_bucket_lifecycle.releases
  lifecycle { destroy = false }
}

removed {
  from = cloudflare_r2_managed_domain.releases
  lifecycle { destroy = false }
}

removed {
  from = cloudflare_r2_custom_domain.releases
  lifecycle { destroy = false }
}
