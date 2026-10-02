# The platform's images, tagged by release version and immutable: a rollback
# names an older version, so tagged images stay. Ship pushes them with the
# github root's release role.
resource "aws_ecr_repository" "release" {
  for_each             = toset(["server", "scheduler", "web"])
  name                 = "${var.name}/release/${each.key}"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "release" {
  for_each   = aws_ecr_repository.release
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after 14 days"
      selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 14 }
      action       = { type = "expire" }
    }]
  })
}

# Workload images: builds push images by digest, BuildKit caches by mutable
# tag and snapshots push per workspace, under
# <name>/workload-images/{images,cache,filesystems/<workspace>}. ECR creates
# each repository on its first push from this template. No expiry rule:
# releases pin images by digest, untagged, and their retention is the
# images owner's.
resource "aws_ecr_repository_creation_template" "workload_images" {
  prefix               = "${var.name}/workload-images"
  description          = "Workload images, build caches and filesystem snapshots, created on first push."
  applied_for          = ["CREATE_ON_PUSH"]
  image_tag_mutability = "MUTABLE"

  encryption_configuration {
    encryption_type = "AES256"
  }
}
