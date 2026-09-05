variable "TAG" {
  default = "local"
}

group "default" {
  targets = [
    "api",
    "scheduler",
    "tunnel-gateway",
    "cache-server",
    "worker-bootstrap",
    "cli",
    "database-bootstrap",
    "agent",
    "container-worker",
  ]
}

group "control-plane" {
  targets = [
    "api",
    "scheduler",
    "tunnel-gateway",
    "cache-server",
    "worker-bootstrap",
    "cli",
    "database-bootstrap",
  ]
}

target "_control-plane" {
  context    = "."
  dockerfile = "docker/Dockerfile.control-plane"
}

target "api" {
  inherits = ["_control-plane"]
  target   = "api"
  tags     = ["api:${TAG}"]
}

target "scheduler" {
  inherits = ["_control-plane"]
  target   = "scheduler"
  tags     = ["scheduler:${TAG}"]
}

target "tunnel-gateway" {
  inherits = ["_control-plane"]
  target   = "tunnel-gateway"
  tags     = ["tunnel-gateway:${TAG}"]
  platforms = ["linux/amd64"]
  output = ["type=image,rewrite-timestamp=true"]
  attest = ["type=provenance,mode=min"]
  args = {
    SOURCE_DATE_EPOCH = "0"
  }
}

target "cache-server" {
  inherits = ["_control-plane"]
  target   = "cache-server"
  tags     = ["cache-server:${TAG}"]
}

target "worker-bootstrap" {
  inherits = ["_control-plane"]
  target   = "worker-bootstrap"
  tags     = ["worker-bootstrap:${TAG}"]
}

target "cli" {
  inherits = ["_control-plane"]
  target   = "cli"
  tags     = ["cli:${TAG}"]
}

target "database-bootstrap" {
  inherits = ["_control-plane"]
  target   = "database-bootstrap"
  tags     = ["database-bootstrap:${TAG}"]
}

target "agent" {
  context    = "."
  dockerfile = "docker/Dockerfile.agent"
  target     = "agent"
  tags       = ["agent:${TAG}"]
}

target "container-worker" {
  context    = "."
  dockerfile = "docker/Dockerfile.worker"
  target     = "container-worker"
  tags       = ["container-worker:${TAG}"]
}
