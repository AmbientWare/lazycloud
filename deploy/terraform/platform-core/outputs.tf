# Read by platform-deployment and github through terraform_remote_state.

output "cluster_name" {
  value = aws_eks_cluster.control_plane.name
}

output "region" {
  value = var.region
}

output "vpc_cidr_block" {
  value = aws_vpc.cluster.cidr_block
}

output "ecr_registry" {
  value = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

output "release_registry" {
  description = "Registry path of the release images, <registry>/<name>/release."
  value       = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com/${var.name}/release"
}

output "release_repositories" {
  description = "Repository ARN per release image."
  value       = { for name, repository in aws_ecr_repository.release : name => repository.arn }
}

output "workload_image_repository" {
  description = "Path under the registry of every workload image repository."
  value       = aws_ecr_repository_creation_template.workload_images.prefix
}

output "oidc_provider_arn" {
  value = aws_iam_openid_connect_provider.cluster.arn
}

output "oidc_issuer_host" {
  value = trimprefix(aws_eks_cluster.control_plane.identity[0].oidc[0].issuer, "https://")
}
