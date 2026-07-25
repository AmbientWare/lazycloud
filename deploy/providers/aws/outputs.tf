output "control_plane_public_ip" {
  value = aws_instance.control_plane.public_ip
}

output "control_plane_private_ip" {
  value = aws_instance.control_plane.private_ip
}

output "object_bucket_name" {
  value = aws_s3_bucket.objects.bucket
}

output "image_bucket_name" {
  value = aws_s3_bucket.images.bucket
}

output "database_address" {
  value = aws_db_instance.database.address
}

output "object_access_key_id" {
  value     = aws_iam_access_key.object_store.id
  sensitive = true
}

output "object_secret_access_key" {
  value     = aws_iam_access_key.object_store.secret
  sensitive = true
}

output "database_password" {
  value     = random_password.database.result
  sensitive = true
}

output "chart_installed" {
  value = var.install_chart
}
