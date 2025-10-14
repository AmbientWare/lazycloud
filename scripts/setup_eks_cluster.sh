#!/bin/bash
set -e

echo "Setting up LocalStack EKS with existing Kubernetes cluster..."

# Wait for LocalStack to be ready
sleep 5

# When using EKS_K8S_PROVIDER=local, LocalStack uses the existing cluster
# We just need to create the EKS cluster resource in LocalStack
echo "Creating EKS cluster resource in LocalStack..."
awslocal eks create-cluster \
  --name lazycloud-cluster \
  --role-arn "arn:aws:iam::000000000000:role/eks-service-role" \
  --resources-vpc-config '{}' || {
    echo "Cluster might already exist, checking status..."
    awslocal eks describe-cluster --name lazycloud-cluster
}

# Wait for cluster to be active
echo "Waiting for EKS cluster to be registered..."
for i in {1..30}; do
  STATUS=$(awslocal eks describe-cluster --name lazycloud-cluster --query 'cluster.status' --output text 2>/dev/null || echo "CREATING")
  if [ "$STATUS" = "ACTIVE" ]; then
    echo "EKS cluster is active!"
    break
  fi
  echo "Cluster status: $STATUS. Waiting... ($i/30)"
  sleep 2
done

# When using EKS_K8S_PROVIDER=local, LocalStack uses the existing kubeconfig
# No need to update it - just verify it exists
if [ -f /root/.kube/config ]; then
  echo "✓ Using existing kubeconfig from host (read-only mount)"
else
  echo "⚠ Kubeconfig not found at /root/.kube/config"
fi

echo "EKS cluster setup complete!"

# Install kubectl in LocalStack container
echo "Installing kubectl..."
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
chmod +x kubectl
mv kubectl /usr/local/bin/

# Verify kubectl is installed and can connect
kubectl version --client --short || echo "Warning: kubectl installation might have failed"

# Test connection to the existing cluster
echo "Testing connection to Kubernetes cluster..."
if kubectl get nodes 2>/dev/null; then
    echo "✓ Successfully connected to existing Kubernetes cluster"
    kubectl get nodes
else
    echo "⚠ Could not connect to Kubernetes cluster. Please ensure:"
    echo "  - Your Minikube cluster is running (minikube status)"
    echo "  - The kubeconfig is properly mounted"
    echo "  - The cluster uses X509 client certificate authentication"
fi

# Create mock gVisor RuntimeClass for local development
echo "Creating mock gVisor RuntimeClass..."
kubectl apply -f - <<EOF 2>/dev/null || echo "Note: RuntimeClass might already exist"
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
  annotations:
    description: "Mock gVisor runtime for local development - uses runc"
handler: runc
EOF

# Verify RuntimeClass was created
if kubectl get runtimeclass gvisor 2>/dev/null; then
    echo "✓ Mock gVisor RuntimeClass is available"
else
    echo "✗ Failed to create mock gVisor RuntimeClass"
fi

echo "Setup complete!"