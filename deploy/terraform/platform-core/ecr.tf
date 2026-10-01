# Repositories of the reference platform's images. Its releases name these
# by commit tag, so restoring main's deployment needs them until the new
# platform is accepted and they are retired with the rest of its pieces
# (tasks/deploy.md, Decommission). The new platform's server, scheduler and
# web images live under <name>/release/ (deploy/terraform/images).
resource "aws_ecr_repository" "image" {
  for_each = toset([
    "api",
    "scheduler",
    "connection-gateway",
    "cache-server",
    "worker-bootstrap", # Retain published images after retiring the bootstrap process.
    "cli",
    "database-bootstrap",
  ])

  name                 = "${var.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.destroy_repositories_with_images

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "workload_images" {
  name                 = "${var.name}/workload-images"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.destroy_repositories_with_images

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Untagged images accumulate on every rebuild of an unchanged layer set. Tagged
# images are kept: a deployment names its tag, and a rollback is a values commit
# naming an older one, which has to still be there.
resource "aws_ecr_lifecycle_policy" "image" {
  for_each = aws_ecr_repository.image

  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after 14 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 14
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "workload_images" {
  repository = aws_ecr_repository.workload_images.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged workload manifests after 14 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 14
      }
      action = { type = "expire" }
    }]
  })
}

# The platform's workload images: builds push images by digest, BuildKit
# caches by mutable tag, and snapshots per workspace filesystem, under
# <name>/workload-images/{images,cache,filesystems/<workspace>}. ECR makes
# each repository on its first push from this template, which the server's
# role may do (platform-deployment iam.tf). No lifecycle rule: releases pin
# images by digest, untagged, and their retention belongs to the images
# owner.
resource "aws_ecr_repository_creation_template" "workload_images" {
  prefix               = "${var.name}/workload-images"
  description          = "Workload images, build caches and filesystem snapshots, created on first push."
  applied_for          = ["CREATE_ON_PUSH"]
  image_tag_mutability = "MUTABLE"

  encryption_configuration {
    encryption_type = "AES256"
  }
}
