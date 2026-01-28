#!/usr/bin/env bash
# Validates that all infrastructure components are running.
# Usage: ./infrastructure/validate.sh

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}✓${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; ERRORS=$((ERRORS + 1)); }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }

ERRORS=0

echo "=== Nodes ==="
if kubectl get nodes -o wide 2>/dev/null; then
  READY=$(kubectl get nodes --no-headers 2>/dev/null | grep -c " Ready " || true)
  TOTAL=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
  if [[ "$READY" -eq "$TOTAL" && "$TOTAL" -gt 0 ]]; then
    pass "All $TOTAL nodes Ready"
  else
    fail "$READY/$TOTAL nodes Ready"
  fi
else
  fail "Cannot reach cluster (check KUBECONFIG)"
  exit 1
fi
echo ""

echo "=== Cilium CNI ==="
if kubectl get ds cilium -n kube-system --no-headers 2>/dev/null | grep -q .; then
  DESIRED=$(kubectl get ds cilium -n kube-system -o jsonpath='{.status.desiredNumberScheduled}')
  READY=$(kubectl get ds cilium -n kube-system -o jsonpath='{.status.numberReady}')
  if [[ "$READY" -eq "$DESIRED" ]]; then
    pass "Cilium DaemonSet $READY/$DESIRED ready"
  else
    fail "Cilium DaemonSet $READY/$DESIRED ready"
  fi
else
  fail "Cilium DaemonSet not found"
fi
echo ""

echo "=== gVisor ==="
if kubectl get runtimeclass gvisor --no-headers 2>/dev/null | grep -q .; then
  pass "RuntimeClass 'gvisor' exists"
else
  fail "RuntimeClass 'gvisor' not found"
fi
echo ""

echo "=== JuiceFS CSI ==="
if kubectl get pods -n kube-system -l app.kubernetes.io/name=juicefs-csi-driver --no-headers 2>/dev/null | grep -q .; then
  RUNNING=$(kubectl get pods -n kube-system -l app.kubernetes.io/name=juicefs-csi-driver --no-headers 2>/dev/null | grep -c "Running" || true)
  TOTAL=$(kubectl get pods -n kube-system -l app.kubernetes.io/name=juicefs-csi-driver --no-headers 2>/dev/null | wc -l)
  if [[ "$RUNNING" -eq "$TOTAL" ]]; then
    pass "JuiceFS CSI pods $RUNNING/$TOTAL running"
  else
    warn "JuiceFS CSI pods $RUNNING/$TOTAL running"
  fi
else
  fail "JuiceFS CSI pods not found"
fi
echo ""

echo "=== Storage Classes ==="
for SC in juicefs-standard juicefs-shared; do
  if kubectl get sc "$SC" --no-headers 2>/dev/null | grep -q .; then
    pass "$SC"
  else
    fail "$SC not found"
  fi
done
echo ""

echo "=== Secrets ==="
for SECRET in "kube-system/juicefs-secret" "kube-system/hetzner-cloud-token" "external-secrets-system/aws-sm-credentials"; do
  NS="${SECRET%/*}"
  NAME="${SECRET#*/}"
  if kubectl get secret "$NAME" -n "$NS" --no-headers 2>/dev/null | grep -q .; then
    pass "$SECRET"
  else
    fail "$SECRET not found"
  fi
done
echo ""

echo "=== ArgoCD ==="
if kubectl get ns argocd --no-headers 2>/dev/null | grep -q .; then
  RUNNING=$(kubectl get pods -n argocd --no-headers 2>/dev/null | grep -c "Running" || true)
  TOTAL=$(kubectl get pods -n argocd --no-headers 2>/dev/null | wc -l)
  if [[ "$RUNNING" -eq "$TOTAL" && "$TOTAL" -gt 0 ]]; then
    pass "ArgoCD pods $RUNNING/$TOTAL running"
  else
    warn "ArgoCD pods $RUNNING/$TOTAL running"
  fi

  # Show ArgoCD apps if any
  APPS_JSON=$(kubectl get applications -n argocd -o json 2>/dev/null || true)
  APP_COUNT=$(echo "$APPS_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))" 2>/dev/null || echo 0)
  if [[ "$APP_COUNT" -gt 0 ]]; then
    echo ""
    echo "  ArgoCD Applications:"
    echo "$APPS_JSON" | python3 -c "
import sys, json
for app in json.load(sys.stdin).get('items', []):
    name = app['metadata']['name']
    sync = app.get('status',{}).get('sync',{}).get('status','Unknown')
    health = app.get('status',{}).get('health',{}).get('status','Unknown')
    print(f'{name} {sync} {health}')
" 2>/dev/null | while read -r NAME SYNC HEALTH; do
      if [[ "$SYNC" == "Synced" && "$HEALTH" == "Healthy" ]]; then
        pass "$NAME ($SYNC / $HEALTH)"
      else
        warn "$NAME ($SYNC / $HEALTH)"
      fi
    done
  fi
else
  fail "ArgoCD namespace not found"
fi
echo ""

echo "=== NGINX Ingress ==="
INGRESS_IP=$(kubectl get svc -n ingress-nginx nginx-ingress-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
if [[ -n "$INGRESS_IP" ]]; then
  pass "NGINX Ingress LB: $INGRESS_IP"
  echo ""
  echo -e "  ${YELLOW}Cloudflare Tunnel origin:${NC} http://$INGRESS_IP:80"
  echo -e "  ${YELLOW}Or DNS A record:${NC} *.lazycloud.dev → $INGRESS_IP"
else
  INGRESS_NODE_PORT=$(kubectl get svc -n ingress-nginx nginx-ingress-controller -o jsonpath='{.spec.ports[?(@.name=="http")].nodePort}' 2>/dev/null || true)
  INGRESS_CLUSTER_IP=$(kubectl get svc -n ingress-nginx nginx-ingress-controller -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
  if [[ -n "$INGRESS_NODE_PORT" ]]; then
    NODE_IP=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null || true)
    pass "NGINX Ingress NodePort: $INGRESS_NODE_PORT"
    echo ""
    echo -e "  ${YELLOW}Cloudflare Tunnel origin:${NC} http://$NODE_IP:$INGRESS_NODE_PORT"
    echo -e "  ${YELLOW}Or DNS A record:${NC} *.lazycloud.dev → $NODE_IP"
  elif [[ -n "$INGRESS_CLUSTER_IP" ]]; then
    pass "NGINX Ingress ClusterIP: $INGRESS_CLUSTER_IP (accessed via Cloudflare Tunnel)"
  else
    warn "NGINX Ingress not found (may not be synced yet)"
  fi
fi
echo ""

echo "=== Cloudflare Tunnel ==="
TUNNEL_ID=$(kubectl logs -n cloudflare-system -l app.kubernetes.io/name=cloudflare-tunnel --tail=50 2>/dev/null | grep "Starting tunnel" | tail -1 | sed 's/.*tunnelID=//' || true)
if [[ -n "$TUNNEL_ID" ]]; then
  pass "Cloudflare Tunnel: $TUNNEL_ID"
  echo ""
  echo -e "  ${YELLOW}CNAME target:${NC} ${TUNNEL_ID}.cfargotunnel.com"
  echo -e "  ${YELLOW}Required DNS records:${NC}"
  echo -e "    lazycloud.dev      → CNAME → ${TUNNEL_ID}.cfargotunnel.com"
  echo -e "    *.lazycloud.dev    → CNAME → ${TUNNEL_ID}.cfargotunnel.com"
else
  warn "Cloudflare Tunnel not running or no tunnel ID found"
fi
echo ""

echo "=== Helm Releases ==="
helm list -A -o json 2>/dev/null | python3 -c "
import sys, json
for r in json.load(sys.stdin):
    print(r['name'], r['namespace'], r['status'])
" 2>/dev/null | while read -r NAME NS STATUS; do
  if [[ "$STATUS" == "deployed" ]]; then
    pass "$NAME ($NS)"
  else
    fail "$NAME ($NS) — $STATUS"
  fi
done
echo ""

if [[ "$ERRORS" -gt 0 ]]; then
  echo -e "${RED}$ERRORS issue(s) found${NC}"
  exit 1
else
  echo -e "${GREEN}All checks passed${NC}"
fi
