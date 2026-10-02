# shellcheck shell=sh
# Releases and digests of what hosts carry beside the agent bundle. Sourced
# by deploy/local/host-setup.sh and prepended to the node image recipe by
# deploy/ami/bake.sh, so local hosts and fleet images run the same gVisor.
# shellcheck disable=SC2034 # read by the scripts that source this file

# gVisor: runsc, its containerd shim and gvisor-bin/, as one tarball per
# architecture, sha512.
GVISOR_RELEASE=20260928
GVISOR_SHA512_X86_64=c8d3a9fd4d4c4f5b8ff213caa4517356be128d18659ec4cde37828fe797f61a9725a602a846c81a8ed19c057a996515d31c081eba343ed4613a89951ba32ed59
GVISOR_SHA512_AARCH64=926538a4f20056d44838f230297ecec9192db2562e2523a207295f706b76126725f2ff7b4e59d747147510c5714057eeec862a0f77d43bf625746592b5f51b00

# Amazon Linux 2023 packages neither qemu-storage-daemon nor nbd-client, which
# the agent's disk engine runs, so node images build them from these sources,
# sha256.
QEMU_VERSION=9.2.3
QEMU_SHA256=baed494270c361bf69816acc84512e3efed71c7a23f76691642b80bc3de7693e
NBD_VERSION=3.26.1
NBD_SHA256=f0cf509fa5b20b1a07f7904eb637e9b47d3e30b6ed6f00075af5d8b701c78fef

# The NVIDIA driver of GPU node images. gVisor's nvproxy accepts only driver
# ABIs its release knows (runsc nvproxy list-supported-drivers); the bake
# refuses an image whose driver it does not.
NVIDIA_DRIVER_STREAM=590-open
NVIDIA_DRIVER_VERSION=590.48.01
NVIDIA_DRIVER_RELEASE=1.amzn2023
