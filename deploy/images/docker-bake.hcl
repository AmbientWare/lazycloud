# Run from the repository root:
#   docker buildx bake -f deploy/images/docker-bake.hcl
# Ship sets REGISTRY, VERSION and SOURCE_DATE_EPOCH (the commit time) and
# pushes the server, scheduler, web and Python base images; locally the
# images load into Docker as lazycloud/<name>:dev. The agent image is the
# release bundle for hosts that run the agent in a container; the server
# image carries the same bundle as an archive.

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
  targets = ["server", "scheduler", "web", "agent", "python"]
}

# What Ship pushes.
group "release" {
  targets = ["server", "scheduler", "web", "python"]
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

target "web" {
  inherits = ["_common"]
  target   = "web"
  tags     = ["${REGISTRY}/web:${VERSION}"]
}

target "agent" {
  inherits = ["_common"]
  target   = "agent"
  tags     = ["${REGISTRY}/agent:${VERSION}"]
}

# The Python base of each version the platform serves, tagged
# <minor>-<VERSION>; the server's image template names them. Their
# timestamps are fixed rather than the commit's and they carry no
# provenance, whose build times would change the pushed digest, so a
# release that leaves them alone builds the same digests and image
# identities stay. A Docker Engine with the containerd image store would
# otherwise unpack the image, which rewritten timestamps rule out.
target "python" {
  name = "python-${replace(item.minor, ".", "")}"
  matrix = {
    item = [
      { minor = "3.10", release = "3.10.20" },
      { minor = "3.11", release = "3.11.15" },
      { minor = "3.12", release = "3.12.13" },
      { minor = "3.13", release = "3.13.14" },
      { minor = "3.14", release = "3.14.6" },
    ]
  }
  context   = "deploy/images/python"
  platforms = ["linux/amd64"]
  args = {
    PYTHON_VERSION    = item.release
    SOURCE_DATE_EPOCH = "0"
  }
  labels = {
    "org.opencontainers.image.source" = "https://github.com/AmbientWare/lazycloud"
  }
  attest = ["type=provenance,disabled=true"]
  output = ["type=image,rewrite-timestamp=true,unpack=false"]
  tags   = ["${REGISTRY}/python:${item.minor}-${VERSION}"]
}
