output "release_role_arn" {
  description = "The role Ship pushes images with."
  value       = aws_iam_role.release.arn
}

output "node_image_role_arn" {
  description = "The role the Node images workflow bakes with."
  value       = aws_iam_role.node_images.arn
}

output "repository_urls" {
  description = "Repository of each image."
  value       = { for image, repository in aws_ecr_repository.image : image => repository.repository_url }
}
