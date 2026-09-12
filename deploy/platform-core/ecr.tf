# One repository per control-plane image, shared by every deployment.
#
# An image is a fact about a commit, not about where it runs. Deploy builds
# each commit once, tags it by SHA, and a deployment names the tag it runs;
# promoting staging to prod moves the name, never the bytes. Per-deployment
# repositories would make promotion a second push of the same image under a
# second name, which is a second thing that can differ.
resource "aws_ecr_repository" "image" {
  for_each = toset([
    "api",
    "scheduler",
    "connection-gateway",
    "cache-server",
    "worker-bootstrap",
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
