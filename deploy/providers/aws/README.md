# AWS Deployment

This Terraform pack provisions VPC networking, K3s control-plane and worker
nodes, RDS PostgreSQL, S3 object/image buckets, object-store credentials, and
optionally installs the service Helm chart into the cluster.

Render and validate locally before applying:

```sh
terraform init
terraform plan \
  -var node_ami=ami-... \
  -var object_bucket_name=lazycloud-objects-example \
  -var image_bucket_name=lazycloud-images-example
```

Chart installation is disabled by default because it requires SSH to
the control-plane node. To install the chart after infrastructure creation, set:

```sh
-var install_chart=true
-var ssh_key_name=your-ec2-keypair
-var ssh_private_key_path=/path/to/key.pem
-var control_plane_public_http_url=https://lazycloud.example.com
-var api_image=registry.example/api
-var scheduler_image=registry.example/scheduler
-var container_worker_image=registry.example/container-worker
-var database_bootstrap_image=registry.example/database-bootstrap
-var storage_gateway_image=registry.example/storage-gateway
-var worker_bootstrap_image=registry.example/worker-bootstrap
-var cache_image=registry.example/cache-server
-var image_tag=latest
```

Terraform creates one namespace-scoped Secret for the generated RDS URL, Redis
URL, S3 credentials, and generated JuiceFS gateway and cache-service
credentials. It uploads the manifest through the existing SSH owner in a
private temporary directory, enforces mode `0600`, validates the exact Helm
render, applies the Secret without printing its contents, and removes the
temporary Secret manifest and Helm values file before returning. Live cloud credentials must stay in
Terraform variables, environment variables, or your normal cloud credential
provider.

Changing any rendered chart value, image input, generated credential, or file
under `deploy/charts/lazycloud` causes the chart installation owner to reconcile
the release again.
