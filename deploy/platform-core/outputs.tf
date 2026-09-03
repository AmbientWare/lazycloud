# Read by `deploy/platform-deployment` through `terraform_remote_state`. Each is
# a fact one deployment needs to attach itself to this cluster; nothing here is
# a secret.

output "cluster_name" {
  description = "Cluster `kubectl` targets and Pod Identity associations name."
  value       = aws_eks_cluster.control_plane.name
}

output "cluster_arn" {
  value = aws_eks_cluster.control_plane.arn
}

output "cluster_endpoint" {
  description = "Kubernetes API endpoint."
  value       = aws_eks_cluster.control_plane.endpoint
}

output "region" {
  description = "Checked against a deployment's own region before it attaches."
  value       = var.region
}

output "vpc_id" {
  value = aws_vpc.cluster.id
}

output "vpc_cidr_block" {
  description = "What a deployment's Redis admits."
  value       = aws_vpc.cluster.cidr_block
}

output "subnet_ids" {
  value = aws_subnet.cluster[*].id
}

output "ecr_registry" {
  description = "Registry every deployment pulls control-plane images from."
  value       = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

output "ecr_repository_prefix" {
  description = "Path under the registry every image repository shares."
  value       = var.name
}

output "ecr_repositories" {
  description = "Repository URL per image name."
  value       = { for name, repository in aws_ecr_repository.image : name => repository.repository_url }
}

output "ecr_repository_arns" {
  description = "What a deployment's deploy role may push and its control plane may pull."
  value       = [for repository in aws_ecr_repository.image : repository.arn]
}

output "workload_image_repository" {
  description = "Immutable OCI repository holding user image layers."
  value       = aws_ecr_repository.workload_images.repository_url
}

output "workload_image_repository_arn" {
  description = "Repository the control plane may vend scoped push and pull credentials for."
  value       = aws_ecr_repository.workload_images.arn
}

output "oidc_provider_arn" {
  value = aws_iam_openid_connect_provider.cluster.arn
}

output "oidc_issuer_host" {
  description = "Issuer without the scheme, which is how IAM condition keys name it."
  value       = trimprefix(aws_eks_cluster.control_plane.identity[0].oidc[0].issuer, "https://")
}

output "storage_class_name" {
  value = kubernetes_storage_class_v1.ebs.metadata[0].name
}

output "argocd_namespace" {
  value = var.argocd_namespace
}
