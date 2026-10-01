output "release_role_arn" {
  description = "The role the release workflow assumes."
  value       = aws_iam_role.release.arn
}

output "repository_urls" {
  description = "Repository of each image."
  value       = { for image, repository in aws_ecr_repository.image : image => repository.repository_url }
}
