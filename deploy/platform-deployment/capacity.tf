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
