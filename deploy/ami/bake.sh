#!/usr/bin/env bash
# Bakes a fleet node image and copies it to every fleet region, printing
# {"<region>": "<ami>"} on stdout and progress on stderr. The Node images
# workflow runs it; locally it needs the platform account's default profile.
# It creates paid EC2 resources: one bake instance, terminated on every exit,
# and the images.
#
# Usage: deploy/ami/bake.sh --variant cpu|gpu [--deployment lazycloud-prod]
#   [--regions us-east-1,us-east-2,us-west-1,us-west-2]
#
# The first region bakes, in a public subnet of the deployment's fleet
# network; the others receive copies. An image of the same recipe that
# already exists is reused, so rerunning an unchanged recipe bakes nothing.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
variant=""
deployment=lazycloud-prod
regions=us-east-1,us-east-2,us-west-1,us-west-2
while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) variant="$2"; shift 2 ;;
    --deployment) deployment="$2"; shift 2 ;;
    --regions) regions="$2"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
case "$variant" in
  cpu) instance_type=c7i.large volume_gib=16 ;;
  # A GPU bake must see a GPU to prove its driver.
  gpu) instance_type=g4dn.xlarge volume_gib=40 ;;
  *) echo "--variant is cpu or gpu" >&2; exit 2 ;;
esac
IFS=, read -r -a region_list <<<"$regions"
bake_region="${region_list[0]}"
export AWS_PAGER=""

log() { echo "$*" >&2; }

# The recipe the instance runs: the pins, the variant, then node-setup.sh.
user_data="$(mktemp)"
trap 'rm -f "$user_data"' EXIT
{
  echo "#!/bin/bash"
  sed '/^#/d' "$root/deploy/host-pins.sh"
  echo "VARIANT=$variant"
  sed 1d "$root/deploy/ami/node-setup.sh"
} >"$user_data"
recipe="$(sha256sum "$user_data" | cut -c1-16)"
name="lazycloud-node-$variant-$recipe"
log "recipe $recipe ($name)"

# shellcheck disable=SC2016 # JMESPath literals, not shell expansions
existing() {
  aws ec2 describe-images --region "$1" --owners self --filters "Name=name,Values=$name" \
    --query 'Images[?State==`available`].ImageId | [0]' --output text
}

ami="$(existing "$bake_region")"
if [[ "$ami" == None || -z "$ami" ]]; then
  base="$(aws ssm get-parameters --region "$bake_region" \
    --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
    --query 'Parameters[0].Value' --output text)"
  subnet="$(aws ec2 describe-subnets --region "$bake_region" --filters \
    "Name=tag:lazycloud:fleet,Values=$deployment" "Name=map-public-ip-on-launch,Values=true" --query 'Subnets[0].SubnetId' --output text)"
  [[ "$subnet" == subnet-* ]] || { log "no public fleet subnet of $deployment in $bake_region"; exit 1; }
  vpc="$(aws ec2 describe-subnets --region "$bake_region" --subnet-ids "$subnet" --query 'Subnets[0].VpcId' --output text)"
  group="$(aws ec2 describe-security-groups --region "$bake_region" --filters "Name=vpc-id,Values=$vpc" \
    "Name=tag:lazycloud:fleet,Values=$deployment" --query 'SecurityGroups[0].GroupId' --output text)"
  log "baking from $base in $subnet"
  tags="ResourceType=instance,Tags=[{Key=lazycloud:node-image-bake,Value=true},{Key=Name,Value=$name}]"
  # Unencrypted, so the image can be shared with connected accounts (an
  # image under the account's default EBS key cannot be). It holds public
  # software only, and every host encrypts its root volume at launch.
  instance="$(aws ec2 run-instances --region "$bake_region" --image-id "$base" --instance-type "$instance_type" \
    --subnet-id "$subnet" --security-group-ids "$group" --associate-public-ip-address \
    --metadata-options HttpTokens=required,HttpEndpoint=enabled \
    --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=$volume_gib,VolumeType=gp3,DeleteOnTermination=true}" \
    --tag-specifications "$tags" "ResourceType=volume,Tags=[{Key=lazycloud:node-image-bake,Value=true}]" \
    --user-data "file://$user_data" --query 'Instances[0].InstanceId' --output text)"
  trap 'aws ec2 terminate-instances --region "$bake_region" --instance-ids "$instance" >/dev/null; rm -f "$user_data"' EXIT
  log "bake instance $instance"

  # The recipe ends by printing a marker to the console; a stop without it
  # is not a finished bake.
  deadline=$((SECONDS + 3600))
  while :; do
    console="$(aws ec2 get-console-output --region "$bake_region" --instance-id "$instance" --latest \
      --query Output --output text 2>/dev/null || true)"
    if grep -q "LAZYCLOUD_BAKE_FAILED" <<<"$console"; then
      grep "LAZYCLOUD_BAKE_FAILED" <<<"$console" >&2
      exit 1
    fi
    grep -q "LAZYCLOUD_BAKE_OK variant=$variant" <<<"$console" && break
    ((SECONDS < deadline)) || { log "the bake did not finish within an hour"; exit 1; }
    sleep 20
  done
  aws ec2 stop-instances --region "$bake_region" --instance-ids "$instance" >/dev/null
  aws ec2 wait instance-stopped --region "$bake_region" --instance-ids "$instance"
  ami="$(aws ec2 create-image --region "$bake_region" --instance-id "$instance" --name "$name" \
    --description "LazyCloud $variant fleet node, recipe $recipe" \
    --tag-specifications "ResourceType=image,Tags=[{Key=lazycloud:node-image,Value=$variant},{Key=lazycloud:recipe,Value=$recipe}]" \
    "ResourceType=snapshot,Tags=[{Key=lazycloud:node-image,Value=$variant}]" \
    --query ImageId --output text)"
  aws ec2 wait image-available --region "$bake_region" --image-ids "$ami"
fi
log "$bake_region $ami"

images="{\"$bake_region\": \"$ami\"}"
for region in "${region_list[@]:1}"; do
  copy="$(existing "$region")"
  if [[ "$copy" == None || -z "$copy" ]]; then
    copy="$(aws ec2 copy-image --region "$region" --source-region "$bake_region" --source-image-id "$ami" \
      --name "$name" --description "LazyCloud $variant fleet node, recipe $recipe" --copy-image-tags \
      --query ImageId --output text)"
    aws ec2 wait image-available --region "$region" --image-ids "$copy"
  fi
  log "$region $copy"
  images="$(jq -c --arg region "$region" --arg ami "$copy" '. + {($region): $ami}' <<<"$images")"
done
echo "$images"
