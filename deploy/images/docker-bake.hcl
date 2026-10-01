# Run from the repository root:
#   docker buildx bake -f deploy/images/docker-bake.hcl
# The release workflow sets REGISTRY, VERSION and SOURCE_DATE_EPOCH (the
# commit time) and pushes; locally the images load into Docker as
# lazycloud/<name>:dev.

variable "REGISTRY" {
  default = "lazycloud"
}

variable "VERSION" {
  default = "dev"
}

# Clamps file and image timestamps so a commit builds the same image digest.
variable "SOURCE_DATE_EPOCH" {
  default = "0"
}

group "default" {
  targets = ["server", "scheduler", "agent"]
}

target "_common" {
  context    = "."
  dockerfile = "deploy/images/Dockerfile"
  platforms  = ["linux/amd64"]
  args = {
    VERSION           = VERSION
    SOURCE_DATE_EPOCH = SOURCE_DATE_EPOCH
  }
  labels = {
    "org.opencontainers.image.source" = "https://github.com/AmbientWare/lazycloud"
  }
  output = ["type=image,rewrite-timestamp=true"]
}

target "server" {
  inherits = ["_common"]
  target   = "server"
  tags     = ["${REGISTRY}/server:${VERSION}"]
}

target "scheduler" {
  inherits = ["_common"]
  target   = "scheduler"
  tags     = ["${REGISTRY}/scheduler:${VERSION}"]
}

target "agent" {
  inherits = ["_common"]
  target   = "agent"
  tags     = ["${REGISTRY}/agent:${VERSION}"]
}
