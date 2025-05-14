#!/bin/bash

# Set variables
VERSION="2204"
DOCKERFILE_PATH="./services/fly/docker_files/ubuntu/${VERSION}/Dockerfile"
IMAGE_NAME="cmclean165/ubuntu${VERSION}"
TAG="latest"

# Go to the machines directory. This is so that the Dockerfile builds correctly
cd machines

# Build the Docker image
echo "Building Docker image..."
docker build -t ${IMAGE_NAME}:${TAG} -f ${DOCKERFILE_PATH} .

# Push the image to Docker Hub
echo "Pushing image to Docker Hub..."
docker push ${IMAGE_NAME}:${TAG}

# Go back to the root directory
cd ..
echo "Done!"
