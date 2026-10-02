#!/bin/sh
# shellcheck shell=busybox
# BUCKET, GARAGE_ADMIN_URL and GARAGE_ADMIN_TOKEN come from compose.yaml.
# shellcheck disable=SC2153
# Assigns the single node's layout, imports the development key and creates the
# bucket. Every step is idempotent.
set -eu -o pipefail

api() {
  method=$1
  op=$2
  shift 2
  curl -fsS -X "$method" -H "Authorization: Bearer $GARAGE_ADMIN_TOKEN" \
    -H 'Content-Type: application/json' "$GARAGE_ADMIN_URL/v2/$op" "$@" | tr -d ' \n'
}

status=$(api GET GetClusterStatus)
node=$(printf '%s' "$status" | sed -n 's/.*"nodes":\[{"id":"\([0-9a-f]*\)".*/\1/p')
layout=$(printf '%s' "$status" | sed -n 's/.*"layoutVersion":\([0-9]*\).*/\1/p')
if [ "$layout" = "0" ]; then
  api POST UpdateClusterLayout -d "{\"roles\":[{\"id\":\"$node\",\"zone\":\"local\",\"capacity\":10000000000,\"tags\":[]}]}" >/dev/null
  api POST ApplyClusterLayout -d '{"version":1}' >/dev/null
fi

if ! api GET "GetKeyInfo?id=$ACCESS_KEY_ID" >/dev/null 2>&1; then
  api POST ImportKey -d "{\"accessKeyId\":\"$ACCESS_KEY_ID\",\"secretAccessKey\":\"$SECRET_ACCESS_KEY\",\"name\":\"lazycloud-local\"}" >/dev/null
fi

if ! bucket=$(api GET "GetBucketInfo?globalAlias=$BUCKET" 2>/dev/null); then
  bucket=$(api POST CreateBucket -d "{\"globalAlias\":\"$BUCKET\"}")
fi
bucket_id=$(printf '%s' "$bucket" | sed -n 's/^{"id":"\([0-9a-f]*\)".*/\1/p')
api POST AllowBucketKey -d "{\"bucketId\":\"$bucket_id\",\"accessKeyId\":\"$ACCESS_KEY_ID\",\"permissions\":{\"read\":true,\"write\":true,\"owner\":true}}" >/dev/null
echo "object store ready: bucket $BUCKET"
