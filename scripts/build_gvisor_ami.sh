#!/bin/bash
set -e

PIPELINE_NAME="lazycloud-gvisor-pipeline"
VALUES_FILE="deploy/platform/karpenter/values-us-east-1.yaml"
POLL_INTERVAL=30

echo "Getting pipeline ARN..."
PIPELINE_ARN=$(aws imagebuilder list-image-pipelines --query "imagePipelineList[?name=='${PIPELINE_NAME}'].arn" --output text)

if [ -z "$PIPELINE_ARN" ]; then
    echo "Error: Pipeline not found. Deploy CDK first."
    exit 1
fi

echo "Starting image build..."
aws imagebuilder start-image-pipeline-execution --image-pipeline-arn "$PIPELINE_ARN"

echo "Waiting for build to complete (this takes ~15-20 minutes)..."

while true; do
    STATUS=$(aws imagebuilder list-image-build-versions \
        --image-version-arn "arn:aws:imagebuilder:us-east-1:$(aws sts get-caller-identity --query Account --output text):image/lazycloud-al2023-gvisor/1.0.0" \
        --query "imageSummaryList[0].state.status" --output text 2>/dev/null || echo "PENDING")

    echo "Status: $STATUS"

    case $STATUS in
        AVAILABLE)
            echo "Build complete!"
            break
            ;;
        FAILED|CANCELLED)
            echo "Build failed with status: $STATUS"
            exit 1
            ;;
        *)
            sleep $POLL_INTERVAL
            ;;
    esac
done

echo "Getting AMI ID..."
AMI_ID=$(aws ec2 describe-images --owners self --filters "Name=tag:Runtime,Values=gvisor" --query "Images | sort_by(@, &CreationDate) | [-1].ImageId" --output text)

if [ -z "$AMI_ID" ] || [ "$AMI_ID" = "None" ]; then
    echo "Error: Could not find AMI"
    exit 1
fi

echo "AMI ID: $AMI_ID"

echo "Updating $VALUES_FILE..."
if grep -q "^gvisorAmiId:" "$VALUES_FILE"; then
    sed -i "s/^gvisorAmiId:.*/gvisorAmiId: $AMI_ID/" "$VALUES_FILE"
else
    echo "gvisorAmiId: $AMI_ID" >> "$VALUES_FILE"
fi

echo "Done! AMI ID $AMI_ID added to $VALUES_FILE"
echo ""
echo "Next steps:"
echo "  1. git add -A && git commit -m 'Update gVisor AMI' && git push"
echo "  2. kubectl delete nodes -l runtime=gvisor"
