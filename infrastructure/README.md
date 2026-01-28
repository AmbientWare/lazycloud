# Hetzner Infrastructure

## Prerequisites
- Hetzner Cloud account with API token (read/write)
- JuiceFS Cloud account (metadata service) + AWS S3 bucket (data). Bucket and file systme should be pre configured and created.
- Packer
- Terraform >= 1.8
- helm, kubectl installed
- AWS account with SecretsManager access (same credentials used for JuiceFS S3 bucket)

## 1. Build Talos OS Snapshot
The cluster runs Talos Linux with gVisor baked in. One-time step (re-run only when upgrading Talos or changing extensions).

```bash
export HCLOUD_TOKEN="YOUR_TOKEN"
./infrastructure/build-snapshot.sh
```

## 2. Create `terraform.tfvars`
```bash
cd infrastructure/terraform
cat > terraform.tfvars <<EOF
hcloud_token              = "YOUR_HCLOUD_TOKEN"
aws_access_key_id         = "YOUR_AWS_ACCESS_KEY"
aws_secret_access_key     = "YOUR_AWS_SECRET_KEY"
juicefs_name              = "lazycloud-prod"
juicefs_token             = "YOUR_JUICEFS_TOKEN"
argocd_repo_ssh_key_path  = "~/.ssh/id_rsa"
EOF
chmod 600 terraform.tfvars
```

## 3. Provision Everything
Single `terraform apply` provisions the Hetzner cluster **and** bootstraps all platform components:
- Cilium CNI
- JuiceFS CSI driver + credentials
- Storage classes (`juicefs-standard`, `juicefs-shared`)
- gVisor RuntimeClass
- Kubernetes secrets (Hetzner token, AWS credentials)
- AWS Secrets Manager secrets
- ArgoCD (with ingress at `argocd.lazycloud.dev`)

```bash
cd infrastructure/terraform
terraform init
terraform apply
```

Then activate kubectl/talosctl:
```bash
source ./infrastructure/kubesetup.sh
```

## 4. Populate AWS Secrets Manager
Terraform creates empty secret containers. Populate them from backups:

```bash
aws secretsmanager put-secret-value --secret-id lazycloud/prod-secrets \
  --secret-string file://../../infrastructure/secrets-backup/lazycloud-prod-secrets.json

aws secretsmanager put-secret-value --secret-id lazycloud/shared-secrets \
  --secret-string file://../../infrastructure/secrets-backup/lazycloud-shared-secrets.json

aws secretsmanager put-secret-value --secret-id lazycloud/staging-secrets \
  --secret-string file://../../infrastructure/secrets-backup/lazycloud-staging-secrets.json
```

## 5. Deploy Platform via ArgoCD
The ApplicationSet deploys all platform charts from `deploy/platform/`.
```bash
kubectl apply -f deploy/argocd-apps/applicationsets/platform.yaml -n argocd
```

## 6. Update Billing
Creates/updates meters (CPU, Memory, Build Minutes, Storage) and products (Developer, Pro, Scale) in Polar.
```bash
uv run update-billing
```

## 7. DNS Cutover
- Update Cloudflare tunnel to point to new cluster's NGINX ingress
- Run `./infrastructure/validate.sh` to get the ingress IP
- Verify: `curl -I https://lazycloud.dev`

## 8. Verify
- [ ] Pods running with gVisor (`kubectl get runtimeclass`)
- [ ] JuiceFS CSI pods running (`kubectl get pods -n kube-system -l app=juicefs-csi-driver`)
- [ ] Storage classes created (`kubectl get sc` — should show `juicefs-standard` and `juicefs-shared`)
- [ ] Deploy a test workspace
- [ ] Confirm builds (Depot), storage (PVCs bound via JuiceFS), ingress (public URL works)
- [ ] Confirm usage collection + billing pipeline (storage metered at $0.10/GB-month)

## Teardown & Rebuild
```bash
cd infrastructure/terraform
terraform destroy
# Rebuild snapshot if needed: ../../infrastructure/build-snapshot.sh
# Then repeat steps 2-7
```
