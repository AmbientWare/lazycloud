#!/usr/bin/env bash
# Checks workspace volumes against real S3 and STS in the platform account
# (profile default): a role like the platform's host role, a workspace
# bucket the storage owner creates, a host grant and GeeseFS mounts over
# TLS, and a dotted cloud bucket addressed by path. The connected-account
# check stands profile default-test in for a customer: a connection role
# there with the connection template's bucket permissions, a workspace
# bucket the storage owner creates through it, a host grant through it and
# the bucket's deletion. Run it only with the owner's approval; it creates
# billable resources for a few minutes.
#
# Every resource is named lazycloud-check-<run> or tagged lazycloud:check=<run>
# and removed on exit, failure included. A run that was killed leaves them
# findable by that tag.
#
# Needs the test PostgreSQL (docker compose -f compose.test.yaml up -d --wait
# postgres), Docker with /dev/fuse and bin/geesefs
# (deploy/local/fetch-geesefs.sh).
#
# Usage: acceptance/awscheck/run.sh <platform account id> <connected account id> [region]
set -euo pipefail

usage="usage: $0 <platform account id> <connected account id> [region]"
account=${1:?$usage}
connected=${2:?$usage}
export AWS_PROFILE=default AWS_REGION=${3:-us-east-2}
root=$(cd "$(dirname "$0")/../.." && pwd)

identity=$(aws sts get-caller-identity --output json)
actual=$(jq -r .Account <<<"$identity")
if [[ "$actual" != "$account" ]]; then
  echo "profile default is account $actual, not $account" >&2
  exit 1
fi
actual=$(aws sts get-caller-identity --profile default-test --query Account --output text)
if [[ "$actual" != "$connected" ]]; then
  echo "profile default-test is account $actual, not $connected" >&2
  exit 1
fi
echo "checking as $(jq -r .Arn <<<"$identity") in $AWS_REGION, with $connected as the connected account"

run=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
prefix="lazycloud-check-$run"
role="lazycloud-volume-check-$run"
connection_role="lazycloud-connection-check-$run"
external_id=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
expires=$(date -u -d '+2 hours' +%FT%TZ)
tags="Key=lazycloud:check,Value=$run Key=lazycloud:expires,Value=$expires"

remove_buckets() {
  local profile=$1
  for bucket in $(aws s3api list-buckets --profile "$profile" --query "Buckets[?starts_with(Name, '$prefix')].Name" --output text); do
    echo "removing bucket $bucket ($profile)"
    aws s3 rb "s3://$bucket" --force --profile "$profile" >/dev/null || return 1
  done
}

remove_role() {
  local profile=$1 name=$2
  if aws iam get-role --profile "$profile" --role-name "$name" >/dev/null 2>&1; then
    echo "removing role $name ($profile)"
    aws iam delete-role-policy --profile "$profile" --role-name "$name" --policy-name buckets || return 1
    aws iam delete-role --profile "$profile" --role-name "$name" || return 1
  fi
}

cleanup() {
  status=$?
  set +e
  docker ps -aq --filter "label=lazycloud.check=$run" | xargs -r docker rm -f >/dev/null
  remove_buckets default || status=1
  remove_buckets default-test || status=1
  remove_role default "$role" || status=1
  remove_role default-test "$connection_role" || status=1
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
aws iam put-role-policy --role-name "$role" --policy-name buckets --policy-document "$(jq -nc --arg prefix "$prefix" --arg account "$account" '{
  Version: "2012-10-17",
  Statement: [
    {Effect: "Allow", Action: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
     Resource: "arn:aws:s3:::\($prefix)-*/*", Condition: {StringEquals: {"s3:ResourceAccount": $account}}},
    {Effect: "Allow", Action: ["s3:ListBucket", "s3:ListBucketMultipartUploads"], Resource: "arn:aws:s3:::\($prefix)-*",
     Condition: {StringEquals: {"s3:ResourceAccount": $account}}}
  ]}')"
role_arn="arn:aws:iam::$account:role/$role"

# The connection role trusts the platform account with the external ID and
# holds the connection template's WorkspaceBuckets and WorkspaceObjects
# statements for this run's prefix.
template="$root/internal/compute/connection_template.json"
# shellcheck disable=SC2086 # one tag per word
aws iam create-role --profile default-test --role-name "$connection_role" --max-session-duration 3600 --tags $tags \
  --assume-role-policy-document "$(jq -nc --arg account "$account" --arg external "$external_id" \
    '{Version: "2012-10-17", Statement: [{Effect: "Allow", Principal: {AWS: "arn:aws:iam::\($account):root"}, Action: "sts:AssumeRole",
      Condition: {StringEquals: {"sts:ExternalId": $external}}}]}')" \
  --query Role.Arn --output text >/dev/null
aws iam put-role-policy --profile default-test --role-name "$connection_role" --policy-name buckets --policy-document "$(
  jq -c --arg prefix "$prefix" --arg account "$connected" '{
    Version: "2012-10-17",
    Statement: [.Resources.ConnectionRole.Properties.Policies[].PolicyDocument.Statement[]
      | select(.Sid == "WorkspaceBuckets" or .Sid == "WorkspaceObjects")
      | .Resource |= (.["Fn::Sub"] | sub("\\$\\{AWS::Partition\\}"; "aws") | sub("\\$\\{BucketPrefix\\}"; $prefix) | sub("\\$\\{AWS::AccountId\\}"; $account))
      | .Condition.StringEquals["s3:ResourceAccount"] = $account]
  }' "$template")"
connection_role_arn="arn:aws:iam::$connected:role/$connection_role"

# IAM takes a few seconds to let a new role be assumed.
for _ in $(seq 30); do
  if aws sts assume-role --role-arn "$role_arn" --role-session-name probe --duration-seconds 900 >/dev/null 2>&1 &&
    aws sts assume-role --role-arn "$connection_role_arn" --external-id "$external_id" --role-session-name probe --duration-seconds 900 >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

cd "$root"
LAZYCLOUD_AWS_CHECK_ACCOUNT="$account" LAZYCLOUD_AWS_CHECK_ROLE_ARN="$role_arn" LAZYCLOUD_AWS_CHECK_PREFIX="$prefix" LAZYCLOUD_AWS_CHECK_RUN="$run" \
  LAZYCLOUD_AWS_CHECK_CONNECTED_ACCOUNT="$connected" LAZYCLOUD_AWS_CHECK_CONNECTED_ROLE_ARN="$connection_role_arn" \
  LAZYCLOUD_AWS_CHECK_EXTERNAL_ID="$external_id" \
  go test -count=1 -v -timeout 20m ./acceptance/awscheck
