#!/usr/bin/env bash

# Setup script for CDK infrastructure
# This script exports the necessary environment variables for CDK deployment

# Only set strict mode if script is executed (not sourced)
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    set -euo pipefail
fi

# Parse arguments
AWS_PROFILE=""
COMMAND_ARGS=()

while [[ $# -gt 0 ]]; do
    case $1 in
        -p|--profile)
            AWS_PROFILE="$2"
            shift 2
            ;;
        *)
            COMMAND_ARGS+=("$1")
            shift
            ;;
    esac
done

# Set AWS_PROFILE if specified
if [ -n "$AWS_PROFILE" ]; then
    export AWS_PROFILE
fi

# Get AWS account ID and region from AWS CLI
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=$(aws configure get region || echo "us-east-1")

# Optional: Set organization and domain name
export ORG_NAME=${ORG_NAME:-"lazycloud"}
export DOMAIN_NAME=${DOMAIN_NAME:-"lazycloud.dev"}
export AWS_REGION=${AWS_REGION:-$CDK_DEFAULT_REGION}

# If command arguments are provided, execute them with the environment variables
if [ ${#COMMAND_ARGS[@]} -gt 0 ]; then
    # Running a command - execute it
    "${COMMAND_ARGS[@]}"
else
    # No command - just display the variables
    echo "Environment variables set:"
    [ -n "$AWS_PROFILE" ] && echo "  AWS_PROFILE: $AWS_PROFILE"
    echo "  CDK_DEFAULT_ACCOUNT: $CDK_DEFAULT_ACCOUNT"
    echo "  CDK_DEFAULT_REGION: $CDK_DEFAULT_REGION"
    echo "  ORG_NAME: $ORG_NAME"
    echo "  DOMAIN_NAME: $DOMAIN_NAME"
    echo "  AWS_REGION: $AWS_REGION"
    echo ""
    echo "Usage:"
    echo "  Load into shell:  source ./setup.sh [-p PROFILE]"
    echo "  Run command:      ./setup.sh [-p PROFILE] <command>"
fi
