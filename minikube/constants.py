# Required tools for Minikube setup
REQUIRED_TOOLS = {
    "minikube": "https://minikube.sigs.k8s.io/docs/start/",
    "kubectl": "https://kubernetes.io/docs/tasks/tools/",
    "helm": "https://helm.sh/docs/intro/install/",
}

# Minikube addons to enable
MINIKUBE_ADDONS = [
    "ingress",
    "storage-provisioner",
    "default-storageclass",
    "metrics-server",
    "gvisor",
]

# Monitoring stack configuration
PROMETHEUS_NAMESPACE = "monitoring"
PROMETHEUS_HELM_REPO = "prometheus-community"
PROMETHEUS_HELM_REPO_URL = "https://prometheus-community.github.io/helm-charts"
PROMETHEUS_CHART = "kube-prometheus-stack"

# Storage class YAML definition
STORAGE_CLASS_YAML = """
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: efs-sc
  annotations:
    storageclass.kubernetes.io/is-default-class: "false"
provisioner: k8s.io/minikube-hostpath
parameters:
  type: Directory
volumeBindingMode: Immediate
allowVolumeExpansion: true
reclaimPolicy: Retain
""".strip()
