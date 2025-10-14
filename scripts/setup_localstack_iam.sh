#!/bin/bash
echo "Setting up IAM resources in LocalStack..."

# Create the base IAM role
echo "Creating LazyCloudECRBaseRole..."
aws iam create-role \
    --role-name LazyCloudECRBaseRole \
    --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"AWS": "arn:aws:iam::000000000000:root"},
            "Action": "sts:AssumeRole"
        }]
    }' \
    --endpoint-url $AWS_ENDPOINT_URL 2>/dev/null || echo "Role already exists"

# Attach ECR permissions to the role
echo "Attaching ECR policy..."
aws iam put-role-policy \
    --role-name LazyCloudECRBaseRole \
    --policy-name LazyCloudECRBasePolicy \
    --policy-document '{
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:DescribeImages"
                ],
                "Resource": "arn:aws:ecr:us-east-1:000000000000:repository/*"
            },
            {
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:CreateRepository",
                    "ecr:DescribeRepositories",
                    "ecr:PutLifecyclePolicy",
                    "ecr:TagResource"
                ],
                "Resource": "*"
            }
        ]
    }' \
    --endpoint-url $AWS_ENDPOINT_URL

echo "✓ IAM setup complete for LocalStack"
echo ""
echo "You can now use the ECR authentication service with LocalStack!"