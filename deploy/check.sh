#!/bin/sh
# Lints and renders the Helm chart, validates the manifests against the
# Kubernetes and External Secrets schemas, and checks the Terraform, using
# pinned tool images. Nothing here reaches AWS or a cluster.
set -eu
cd "$(dirname "$0")/.."

helm=docker.io/alpine/helm:4.3.0@sha256:a6cf54599ccb99d90cf0712b30f03fdb3cab062e6b94e0418cc4db7e8a1464b2
kubeconform=ghcr.io/yannh/kubeconform:v0.8.0@sha256:faffaf43f95aa6425306e1ab8d6fcad72acb9049158f38e574c085ea1ec0f64e
terraform=docker.io/hashicorp/terraform:1.16.4@sha256:985cdc6c1d9b0a65b83377f666efd2f740b47f02ac55be1ced3d18f7d3b0e829
kubernetes=1.36.0
k8s_schemas="https://raw.githubusercontent.com/yannh/kubernetes-json-schema/8df8a883b68a24a104b4a9e43c1288090ae60b3b/{{.NormalizedKubernetesVersion}}-standalone{{.StrictSuffix}}/{{.ResourceKind}}{{.KindSuffix}}.json"
crd_schemas="https://raw.githubusercontent.com/datreeio/CRDs-catalog/d373c2da9702bc9509a004db83e57263fe3bdfc1/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"

out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
user="$(id -u):$(id -g)"

run_helm() {
  docker run --rm --user "$user" -e HOME=/tmp -v "$PWD/deploy/helm:/charts:ro" "$helm" "$@"
}

# The example deployment, and the chart defaults with only the image set.
run_helm lint --strict /charts/lazycloud -f /charts/example-values.yaml
run_helm template lazycloud /charts/lazycloud -f /charts/example-values.yaml \
  --namespace lazycloud-example >"$out/example.yaml"
run_helm template lazycloud /charts/lazycloud --set image.registry=registry.example.com \
  --set image.tag=v0.1.0 --namespace lazycloud-example >"$out/defaults.yaml"

docker run --rm -v "$out:/manifests:ro" "$kubeconform" -strict -summary \
  -kubernetes-version "$kubernetes" -schema-location "$k8s_schemas" -schema-location "$crd_schemas" \
  /manifests/example.yaml /manifests/defaults.yaml

for root in deploy/terraform/*/; do
  docker run --rm --user "$user" -e HOME=/tmp -e TF_IN_AUTOMATION=1 -v "$PWD/$root:/root-module" \
    -w /root-module "$terraform" fmt -check -diff
  docker run --rm --user "$user" -e HOME=/tmp -e TF_IN_AUTOMATION=1 -v "$PWD/$root:/root-module" \
    -w /root-module "$terraform" init -backend=false -input=false -lockfile=readonly >/dev/null
  docker run --rm --user "$user" -e HOME=/tmp -e TF_IN_AUTOMATION=1 -v "$PWD/$root:/root-module" \
    -w /root-module "$terraform" validate
done
echo "deploy checks passed"
