#!/bin/sh
# Executes every sqlc query through the production Neki router on a scratch
# database (lc_neki_check) on the same branch, then drops it. Prints no
# credentials. Usage: acceptance/neki/check.sh [-v]
set -eu
cd "$(dirname "$0")/../.."
NEKI_DATABASE_URL=$(AWS_PROFILE=default aws secretsmanager get-secret-value --region us-east-1 \
  --secret-id lazycloud-prod/platform --query SecretString --output text | jq -r '.LAZYCLOUD_DATABASE_URL')
export NEKI_DATABASE_URL
GOTOOLCHAIN=go1.27.1 exec go run ./acceptance/neki "$@"
