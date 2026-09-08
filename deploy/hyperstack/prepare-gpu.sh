#!/bin/bash
set -Eeuo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg \
  build-essential "linux-headers-$(uname -r)"
curl -fsSL https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb \
  -o /tmp/cuda-keyring.deb
dpkg -i /tmp/cuda-keyring.deb
rm /tmp/cuda-keyring.deb
apt-get update
apt-get install -y nvidia-driver-pinning-590.48.01=590.48.01-0ubuntu1
apt-get install -y --no-install-recommends nvidia-open=590.48.01-0ubuntu1
modprobe nvidia
modprobe nvidia_uvm

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update
apt-get install -y nvidia-container-toolkit=1.20.0-1 \
  nvidia-container-toolkit-base=1.20.0-1 libnvidia-container-tools=1.20.0-1 \
  libnvidia-container1=1.20.0-1
nvidia-ctk runtime configure --runtime=docker

test "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | sort -u)" = 590.48.01
test "$(nvidia-smi --query-gpu=uuid --format=csv,noheader | wc -l)" -eq 8
nvidia-smi -L

runsc_download=$(mktemp -d)
curl -fsSL "https://storage.googleapis.com/gvisor/releases/release/${GVISOR_VERSION}/x86_64/runsc" \
  -o "$runsc_download/runsc"
curl -fsSL "https://storage.googleapis.com/gvisor/releases/release/${GVISOR_VERSION}/x86_64/runsc.sha512" \
  -o "$runsc_download/runsc.sha512"
(cd "$runsc_download" && sha512sum -c runsc.sha512)
install -m 0755 "$runsc_download/runsc" /usr/local/bin/runsc
rm -r "$runsc_download"
runsc nvproxy list-supported-drivers | grep -F '590.48.01'
runsc install -- --nvproxy=true
systemctl restart docker

probe_image=nvcr.io/nvidia/k8s/cuda-sample@sha256:95ce52d6e3b11783606152f4da94af9cf84e7ca4dd63eb03c95edcc5b7bba8d9
while IFS= read -r gpu_uuid; do
  docker run --runtime=runsc --gpus "device=$gpu_uuid" --rm "$probe_image"
done < <(nvidia-smi --query-gpu=uuid --format=csv,noheader)
docker image rm "$probe_image"

# Workers carry their own runsc. This host copy only validates the baked driver.
runsc uninstall
rm /usr/local/bin/runsc
systemctl restart docker
