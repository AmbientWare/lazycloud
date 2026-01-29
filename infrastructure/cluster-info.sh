#!/usr/bin/env bash
# Shows cluster setup info
# Usage: ./infrastructure/cluster-info.sh

set -e

echo "=== Cluster Info ==="
echo

# ArgoCD password
echo "ArgoCD Admin Password:"
ARGOCD_PASS=$(kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)
if [ -n "$ARGOCD_PASS" ]; then
  echo "  $ARGOCD_PASS"
else
  echo "  (not available yet)"
fi
echo

# Cloudflare Tunnel info
echo "=== Cloudflare Tunnel ==="
TUNNEL_POD=$(kubectl get pods -n cloudflare-system -l app.kubernetes.io/name=cloudflare-tunnel -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "$TUNNEL_POD" ]; then
  TUNNEL_LOGS=$(kubectl logs -n cloudflare-system "$TUNNEL_POD" --tail=50 2>/dev/null)
  TUNNEL_ID=$(echo "$TUNNEL_LOGS" | grep -oP 'tunnelID=\K[a-f0-9-]+' | head -1)
  CONNECTIONS=$(echo "$TUNNEL_LOGS" | grep -c "Registered tunnel connection" || echo "0")

  echo "  Tunnel ID:   ${TUNNEL_ID:-unknown}"
  echo "  Status:      $CONNECTIONS connections active"
  echo "  CNAME target: ${TUNNEL_ID}.cfargotunnel.com"
else
  echo "  Tunnel not running"
fi
echo

# Ingresses / DNS needed
echo "=== DNS CNAMEs needed ==="
INGRESSES=$(kubectl get ingress -A -o jsonpath='{range .items[*]}{.spec.rules[*].host}{"\n"}{end}' 2>/dev/null | sort -u)
TUNNEL_ID=$(kubectl logs -n cloudflare-system deployment/cloudflare-tunnel --tail=20 2>/dev/null | grep -oP 'tunnelID=\K[a-f0-9-]+' | head -1)
if [ -n "$INGRESSES" ] && [ -n "$TUNNEL_ID" ]; then
  echo "$INGRESSES" | while read -r host; do
    [ -n "$host" ] && echo "  $host CNAME ${TUNNEL_ID}.cfargotunnel.com"
  done
else
  echo "  (no ingresses found)"
fi
echo

# App status
echo "=== ArgoCD Applications ==="
kubectl get applications -n argocd --no-headers 2>/dev/null | while read -r line; do
  NAME=$(echo "$line" | awk '{print $1}')
  SYNC=$(echo "$line" | awk '{print $2}')
  HEALTH=$(echo "$line" | awk '{print $3}')
  printf "  %-30s %s / %s\n" "$NAME" "$SYNC" "$HEALTH"
done
echo
