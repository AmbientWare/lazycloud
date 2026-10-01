output "release_role_arn" {
  description = "Set as the images environment variable AWS_IMAGE_RELEASE_ROLE_ARN."
  value       = aws_iam_role.release.arn
}

output "registry" {
  description = "Set as the images environment variable IMAGE_REGISTRY: <registry host>/<name>."
  value       = "${split("/", aws_ecr_repository.image["server"].repository_url)[0]}/${var.name}"
}

output "repository_urls" {
  description = "Repository of each image."
  value = merge(
    { for image, repository in aws_ecr_repository.image : image => repository.repository_url },
    { scheduler = data.aws_ecr_repository.scheduler.repository_url },
  )
}
