from shared.models.billing import STORAGE_CLASS_EFS, STORAGE_CLASS_S3

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
    "csi-hostpath-driver",
]

# Monitoring stack configuration
PROMETHEUS_NAMESPACE = "monitoring"
PROMETHEUS_HELM_REPO = "prometheus-community"
PROMETHEUS_HELM_REPO_URL = "https://prometheus-community.github.io/helm-charts"
PROMETHEUS_CHART = "kube-prometheus-stack"

# Storage class YAML definitions
EFS_STORAGE_CLASS_YAML = f"""
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: {STORAGE_CLASS_EFS}
  annotations:
    storageclass.kubernetes.io/is-default-class: "false"
provisioner: hostpath.csi.k8s.io
volumeBindingMode: Immediate
allowVolumeExpansion: true
reclaimPolicy: Retain
""".strip()


S3_STORAGE_CLASS_YAML = f"""
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: {STORAGE_CLASS_S3}
  annotations:
    storageclass.kubernetes.io/is-default-class: "false"
provisioner: hostpath.csi.k8s.io
volumeBindingMode: Immediate
allowVolumeExpansion: true
reclaimPolicy: Retain
""".strip()
