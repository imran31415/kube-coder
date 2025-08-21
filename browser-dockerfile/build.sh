#!/bin/bash

# Build the browser-enabled image
IMAGE_TAG="registry.digitalocean.com/resourceloop/coder:devlaptop-v1.6.0-browser"

echo "Building browser-enabled coder image..."
docker build -t $IMAGE_TAG .

echo "Pushing to registry..."
docker push $IMAGE_TAG

echo "Image built and pushed: $IMAGE_TAG"
echo ""
echo "To use this image, update your values.yaml:"
echo "image:"
echo "  repository: registry.digitalocean.com/resourceloop/coder"
echo "  tag: devlaptop-v1.6.0-browser"