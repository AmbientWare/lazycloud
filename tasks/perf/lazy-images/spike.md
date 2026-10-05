# Spike: Docker, a remote snapshotter and gVisor

## Scope

Answer the four questions in plan.md with evidence, before any product code.
Scratch code and configuration only; nothing here merges. Owns nothing in
the tree except this file.

## Plan

1. **Local machine.** Skip to step 2 if starting a second dockerd needs a password you do not have. Docker 29.6 with containerd 2.2 runs here. Do not change
   the user's Docker or containerd configuration. Run a separate dockerd and
   containerd pair with their own root, state and socket directories (for
   example under `/tmp/lazy-spike`), started and stopped by you, with the
   containerd image store enabled and the stargz snapshotter registered as a
   proxy plugin. Install pinned releases of stargz-snapshotter and gVisor
   into the scratch directory, checksums verified.
   - Convert a public Python image to eStargz and push it to a scratch
     registry container you start.
   - `docker pull` and `docker run` it through the scratch daemon with runc,
     then with runsc. Record whether layer blobs were downloaded (registry
     logs, containerd content store), whether the container reads its files,
     and its start time.
   - Restart the snapshotter's caller side (stop and start your stand-in for
     the agent, not the snapshotter) while a container reads files, and
     record what the container sees.
2. **EC2 host from the node image.** One on-demand or Spot m7i.large in
   `us-east-2`, `AWS_PROFILE=default`, tagged `lazycloud:task=perf-lazy-spike`,
   launched from the CPU node image the fleet uses (see
   `deploy/helm/lazycloud/environments/prod.yaml` for the image IDs), with no
   agent user data. Repeat step 1's runs there with the host's own Docker
   and runsc, configured the way a node image would carry it.
   - Cold start of the converted Python image on that fresh host against a
     plain `docker pull` of the original, three runs each.
   - The same for a large image (a public PyTorch CPU image).
   - Terminate the instance and anything it created before reporting.
3. Write down the exact daemon and containerd settings that worked, the
   snapshotter's labels and handler behavior Docker relied on, and anything
   gVisor needed (mount options, gofer settings, FUSE device access).

## Evidence to record

For each question: yes or no, the command output that shows it, and the
settings. The cold start numbers as a table. What did not work and why.

## Progress

- Local step skipped: no passwordless sudo, so no second dockerd here.
- EC2 host: m7i.large on demand, us-east-2a, fleet CPU image
  ami-0e3303f49502924b3 (lazycloud-node-cpu-d0afedf4e47c63ba), 40 GiB gp3,
  instance profile lazycloud-prod-fleet-node (SSM only), no agent user data.
  The default VPC in us-east-2 has no internet route, so the spike runs in a
  scratch VPC it creates and deletes.
- The node image carries Docker 25.0.16 (Amazon Linux package) on the
  overlay2 graphdriver, containerd 2.2.7, runc 1.3.6, runsc release-20260928.0,
  kernel 6.18.51, /dev/fuse present. It is not Docker 29.
- Installed on the instance only: stargz-snapshotter v0.18.2 (sha256
  515a3c3a... matches the release's .sha256sum), Docker static 29.8.2 and
  29.9.0-rc.1 (download.docker.com publishes no checksums; pinned by the
  sha256 of an independent download: 995d1ef2..., 408e3702...). Docker 29
  runs against the image's containerd 2.2.7.
- Images: python:3.12-slim (index sha256:02108f5d...) and
  pytorch/torchserve:0.12.0-cpu (index sha256:50e18949..., torch 2.4.0+cpu),
  linux/amd64. The originals are copied byte for byte into a scratch
  registry:3 on the instance (`-org`), and converted with
  `ctr-remote i convert --oci --estargz` (`-esgz`, 14 s and 166 s). Registry
  storage is a tmpfs, so the registry never competes for the root volume.
- Done: baselines on Docker 25 and Docker 29, lazy pulls, gVisor file checks,
  cold-start table.

## Gaps and unverified boundaries
