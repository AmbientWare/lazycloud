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
