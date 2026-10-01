#!/bin/sh
# Lints and renders the Helm chart, validates the manifests and Argo CD
# Applications against their schemas, checks that every setting the chart
# gives a process is one its binary reads, checks the Terraform roots, the
# workflows and the shell scripts, using pinned tool images. Nothing here
# reaches AWS, GitHub or a cluster.
set -eu
cd "$(dirname "$0")/.."

helm=docker.io/alpine/helm:4.3.0@sha256:a6cf54599ccb99d90cf0712b30f03fdb3cab062e6b94e0418cc4db7e8a1464b2
kubeconform=ghcr.io/yannh/kubeconform:v0.8.0@sha256:faffaf43f95aa6425306e1ab8d6fcad72acb9049158f38e574c085ea1ec0f64e
terraform=docker.io/hashicorp/terraform:1.16.4@sha256:985cdc6c1d9b0a65b83377f666efd2f740b47f02ac55be1ced3d18f7d3b0e829
actionlint=docker.io/rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667
shellcheck=docker.io/koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d
kubernetes=1.36.0
k8s_schemas="https://raw.githubusercontent.com/yannh/kubernetes-json-schema/8df8a883b68a24a104b4a9e43c1288090ae60b3b/{{.NormalizedKubernetesVersion}}-standalone{{.StrictSuffix}}/{{.ResourceKind}}{{.KindSuffix}}.json"
crd_schemas="https://raw.githubusercontent.com/datreeio/CRDs-catalog/d373c2da9702bc9509a004db83e57263fe3bdfc1/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"

out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
user="$(id -u):$(id -g)"

run_helm() {
  docker run --rm --user "$user" -e HOME=/tmp -v "$PWD/deploy/helm:/charts:ro" "$helm" "$@"
}

# Production as Argo CD renders it: chart defaults, the environment file and
# a placeholder of the values Deploy writes.
prod="-f /charts/lazycloud/environments/prod.yaml -f /charts/example-deployment.yaml"
# shellcheck disable=SC2086 # the value files are separate words
run_helm lint --strict /charts/lazycloud $prod --kube-version "$kubernetes"
# shellcheck disable=SC2086
run_helm template lazycloud /charts/lazycloud $prod --kube-version "$kubernetes" \
  --namespace lazycloud-prod >"$out/prod.yaml"
# The other branches: telemetry on, no TCP pods, hosts inside the cluster, an
# existing Secret.
# shellcheck disable=SC2086
run_helm template lazycloud /charts/lazycloud $prod --kube-version "$kubernetes" \
  --set telemetry.enabled=true --set telemetry.endpoint=https://otlp.example.com \
  --set server.tcpService.enabled=false --set server.hostService.type=ClusterIP \
  --set externalSecrets.enabled=false --namespace lazycloud-prod >"$out/variant.yaml"

# Settings that must refuse to render.
refuse() {
  reason=$1
  shift
  # shellcheck disable=SC2086
  if run_helm template lazycloud /charts/lazycloud $prod --kube-version "$kubernetes" "$@" >/dev/null 2>&1; then
    echo "the chart rendered $reason" >&2
    exit 1
  fi
}
refuse "a host load balancer without a certificate" --set server.hostService.certificateArn=
refuse "a NodePort host service" --set server.hostService.type=NodePort
refuse "fleet networks without node images" --set config.LAZYCLOUD_FLEET_IMAGES=null
refuse "a chart-owned setting in config" --set config.LAZYCLOUD_PUBLIC_URL=https://elsewhere.example.com
refuse "a secret in config" --set server.config.LAZYCLOUD_GITHUB_CLIENT_SECRET=plain

docker run --rm -v "$out:/manifests:ro" -v "$PWD/deploy/argocd:/argocd:ro" "$kubeconform" -strict -summary \
  -kubernetes-version "$kubernetes" -schema-location "$k8s_schemas" -schema-location "$crd_schemas" \
  /manifests/prod.yaml /manifests/variant.yaml /argocd/apps

# Every LAZYCLOUD_* variable the chart gives a container is one its binary
# reads: the server's and jobs' in cmd/server, the scheduler's in
# cmd/scheduler, or the shared fleet and telemetry settings.
reads() {
  grep -rqsF "\"$2\"" "cmd/$1" internal/compute/fleet_config.go internal/telemetry/env.go
}
for binary in server scheduler; do
  case $binary in
    server) templates="templates/server.yaml templates/migrations.yaml templates/agent-release.yaml" ;;
    scheduler) templates="templates/scheduler.yaml" ;;
  esac
  for template in $templates; do
    # shellcheck disable=SC2086
    run_helm template lazycloud /charts/lazycloud $prod --kube-version "$kubernetes" \
      --set telemetry.enabled=true --set telemetry.endpoint=https://otlp.example.com \
      --show-only "$template" >"$out/env.yaml"
    grep -oE '^ *- name: LAZYCLOUD_[A-Z0-9_]+' "$out/env.yaml" | awk '{print $3}' | sort -u >"$out/names"
    while read -r name; do
      if ! reads "$binary" "$name"; then
        echo "$template sets $name, which $binary does not read" >&2
        exit 1
      fi
    done <"$out/names"
  done
done

for root in deploy/terraform/*/; do
  docker run --rm --user "$user" -e HOME=/tmp -e TF_IN_AUTOMATION=1 -v "$PWD/$root:/root-module" \
    -w /root-module "$terraform" fmt -check -recursive -diff
  docker run --rm --user "$user" -e HOME=/tmp -e TF_IN_AUTOMATION=1 -v "$PWD/$root:/root-module" \
    -w /root-module "$terraform" init -backend=false -input=false -lockfile=readonly >/dev/null
  docker run --rm --user "$user" -e HOME=/tmp -e TF_IN_AUTOMATION=1 -v "$PWD/$root:/root-module" \
    -w /root-module "$terraform" validate
done

docker run --rm -v "$PWD:/repo:ro" -w /repo "$actionlint" -no-color
# shellcheck disable=SC2046 # one word per script
docker run --rm -v "$PWD:/repo:ro" -w /repo "$shellcheck" $(find deploy -name '*.sh' -not -path '*/.terraform/*' | sort)
echo "deploy checks passed"
