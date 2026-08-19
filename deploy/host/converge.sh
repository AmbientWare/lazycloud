#!/bin/bash
# Converge this host onto the current deployment bundle.
#
# Lives in the bundle rather than in user data. User data runs once, at first
# boot, so a script baked into it can only be corrected by replacing the machine
# — and the first three defects here were all in this script. Shipping it with
# the release means a fix reaches the host the same way an image does.
#
# Everything it needs comes from the bundle or from Secrets Manager. It takes no
# arguments and holds no credentials.
set -euo pipefail
umask 077

BUNDLE="${LAZYCLOUD_BUNDLE_URI:?LAZYCLOUD_BUNDLE_URI is required}"
REGION="${LAZYCLOUD_REGION:?LAZYCLOUD_REGION is required}"
REGISTRY="${LAZYCLOUD_REGISTRY:?LAZYCLOUD_REGISTRY is required}"

cd /opt/lazycloud

for artifact in compose.yaml compose.deploy.yaml collector.deploy.yaml images.env runtime.env services; do
  aws s3 cp "$BUNDLE/$artifact" "$artifact"
done

# A secret with no value is normal, not fatal. Several are minted by bootstrap
# and some name a backend nobody has chosen yet, so the deployment comes up with
# what exists and says plainly what is missing. Aborting here instead would mean
# an unset telemetry endpoint stops the control plane from serving.
: >secrets.env
chmod 0600 secrets.env
missing=()
while IFS='=' read -r variable secret; do
  variable="$(printf '%s' "$variable" | tr -d '[:space:]')"
  secret="$(printf '%s' "$secret" | tr -d '[:space:]')"
  [ -n "$variable" ] || continue
  if value="$(aws secretsmanager get-secret-value --secret-id "$secret" \
      --query SecretString --output text 2>/dev/null)" && [ -n "$value" ]; then
    printf '%s=%s\n' "$variable" "$value" >>secrets.env
  else
    printf '%s=\n' "$variable" >>secrets.env
    missing+=("$variable")
  fi
done </opt/lazycloud/secret-map

if [ ${#missing[@]} -gt 0 ]; then
  echo "secrets with no value: ${missing[*]}" >&2
fi

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

read -r -a services <services

COMPOSE=(docker compose -f compose.yaml -f compose.deploy.yaml
  --env-file images.env --env-file runtime.env --env-file secrets.env
  --profile public-ingress)

"${COMPOSE[@]}" pull "${services[@]}"
"${COMPOSE[@]}" up -d --remove-orphans "${services[@]}"
"${COMPOSE[@]}" ps
