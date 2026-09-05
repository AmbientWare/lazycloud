variable "hetzner_node_images" {
  description = "Verified snapshot IDs and recipe digests from Hetzner Node Images. Supply its hetzner-images.tfvars.json artifact when applying this deployment."
  type = map(object({
    image_id      = number
    recipe_sha256 = string
  }))

  validation {
    condition = contains(keys(var.hetzner_node_images), "ash") && alltrue([
      for image in values(var.hetzner_node_images) :
      image.image_id > 0 && floor(image.image_id) == image.image_id && can(regex("^[0-9a-f]{64}$", image.recipe_sha256))
    ])
    error_message = "The default Ashburn provider requires a verified numeric snapshot ID and full SHA-256 recipe digest."
  }
}

variable "hetzner_usd_per_currency_unit" {
  description = "Conversion from the project's API price currency to USD. USD-billed projects use 1."
  type        = number
  default     = 1

  validation {
    condition     = var.hetzner_usd_per_currency_unit > 0
    error_message = "Supplier currency conversion must be positive."
  }
}

variable "hetzner_primary_ipv4_hourly_micros" {
  description = "Supplier IPv4 cost in USD microdollars per hour, added to node offers."
  type        = number
  default     = 1000

  validation {
    condition     = var.hetzner_primary_ipv4_hourly_micros >= 0 && floor(var.hetzner_primary_ipv4_hourly_micros) == var.hetzner_primary_ipv4_hourly_micros
    error_message = "IPv4 cost must be nonnegative whole microdollars."
  }
}

locals {
  hetzner_capacity = [{
    ref       = "hetzner:platform"
    workspace = "default"
    policy = {
      default_region         = "ash"
      allowed_regions        = ["ash"]
      allowed_instance_types = ["ccx33", "ccx43", "ccx53", "ccx63"]
      warm_cpu_min           = 1
    }
    images_by_location         = var.hetzner_node_images
    usd_per_currency_unit      = var.hetzner_usd_per_currency_unit
    primary_ipv4_hourly_micros = var.hetzner_primary_ipv4_hourly_micros
  }]
}
