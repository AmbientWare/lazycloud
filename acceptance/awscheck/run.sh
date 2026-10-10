#!/usr/bin/env bash
# Checks workspace volumes against real S3 and STS in the platform account
# (profile default): a role like the platform's host role, a workspace
# bucket the storage owner creates, a host grant and GeeseFS mounts over
# TLS, and a dotted cloud bucket addressed by path. Run it only with the
# owner's approval; it creates billable resources for a few minutes.
#
# Every resource is named lazycloud-check-<run> or tagged lazycloud:check=<run>
# and removed on exit, failure included. A run that was killed leaves them
# findable by that tag.
#
# Needs the test PostgreSQL (docker compose -f compose.test.yaml up -d --wait
# postgres), Docker with /dev/fuse and bin/geesefs
# (deploy/local/fetch-geesefs.sh).
#
# Usage: acceptance/awscheck/run.sh <platform account id> [region]
set -euo pipefail

account=${1:?usage: $0 <platform account id> [region]}
export AWS_PROFILE=default AWS_REGION=${2:-us-east-2}
root=$(cd "$(dirname "$0")/../.." && pwd)

identity=$(aws sts get-caller-identity --output json)
actual=$(jq -r .Account <<<"$identity")
if [[ "$actual" != "$account" ]]; then
  echo "profile default is account $actual, not $account" >&2
  exit 1
fi
echo "checking as $(jq -r .Arn <<<"$identity") in $AWS_REGION"

run=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
prefix="lazycloud-check-$run"
role="lazycloud-volume-check-$run"
expires=$(date -u -d '+2 hours' +%FT%TZ)
tags="Key=lazycloud:check,Value=$run Key=lazycloud:expires,Value=$expires"

cleanup() {
  status=$?
  set +e
  docker ps -aq --filter "label=lazycloud.check=$run" | xargs -r docker rm -f >/dev/null
  for bucket in $(aws s3api list-buckets --query "Buckets[?starts_with(Name, '$prefix')].Name" --output text); do
    echo "removing bucket $bucket"
    aws s3 rb "s3://$bucket" --force >/dev/null || status=1
  done
  if aws iam get-role --role-name "$role" >/dev/null 2>&1; then
    echo "removing role $role"
    aws iam delete-role-policy --role-name "$role" --policy-name buckets || status=1
    aws iam delete-role --role-name "$role" || status=1
  fi
  exit "$status"
}
trap cleanup EXIT

# The caller's account may assume the role; the role reaches only this
# run's buckets, as the platform's host role reaches workspace buckets and
# host grants narrow it further.
# shellcheck disable=SC2086 # one tag per word
aws iam create-role --role-name "$role" --max-session-duration 3600 --tags $tags \
  --assume-role-policy-document "$(jq -nc --arg account "$account" \
    '{Version: "2012-10-17", Statement: [{Effect: "Allow", Principal: {AWS: "arn:aws:iam::\($account):root"}, Action: "sts:AssumeRole"}]}')" \
  --query Role.Arn --output text >/dev/null
aws iam put-role-policy --role-name "$role" --policy-name buckets --policy-document "$(jq -nc --arg prefix "$prefix" '{
  Version: "2012-10-17",
  Statement: [
    {Effect: "Allow", Action: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
     Resource: "arn:aws:s3:::\($prefix)-*/*"},
    {Effect: "Allow", Action: ["s3:ListBucket", "s3:ListBucketMultipartUploads"], Resource: "arn:aws:s3:::\($prefix)-*"}
  ]}')"
role_arn="arn:aws:iam::$account:role/$role"

# IAM takes a few seconds to let a new role be assumed.
for _ in $(seq 30); do
  if aws sts assume-role --role-arn "$role_arn" --role-session-name probe --duration-seconds 900 >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

cd "$root"
LAZYCLOUD_AWS_CHECK_ACCOUNT="$account" LAZYCLOUD_AWS_CHECK_ROLE_ARN="$role_arn" LAZYCLOUD_AWS_CHECK_PREFIX="$prefix" LAZYCLOUD_AWS_CHECK_RUN="$run" \
  go test -count=1 -v -timeout 20m ./acceptance/awscheck
