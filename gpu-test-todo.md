# Proving the GPU claim locally

`in_process=True` puts a function's concurrent invocations in one interpreter so
a model loaded by `on_start` is loaded once and shared. `scenario_in_process`
proves the property that rests on — 50 invocations, one process, one shared
object — but has never run on a card. This is what it would take to run it on
the RTX 3090 in this machine, written down at the point it was blocked.

## Why it does not work today

The worker runs user containers under `runsc`, and gVisor proxies GPUs only for
driver versions nvproxy has ABI structs for. `runsc nvproxy list-supported-drivers`
covers `590.48.01`, `615.15.00`, `620.06.00` — and nothing in the 595 branch.
This host runs `595.84` from `noble-updates`.

Bumping gVisor does not help: release `20260810.0` supports the same three, so
595 was skipped rather than not-yet-added.

`deploy/ami/bake.py` pins `_NVIDIA_DRIVER_VERSION = "590.48.01"` and installs the
NVIDIA container toolkit, so the platform's own GPU nodes run a driver gVisor
supports. The production path is correct; this desktop is the outlier.

Running GPU containers under `runc` locally was considered and rejected: it is a
path production never takes, which is the divergence the whole change set has
been avoiding.

## Steps

1. **Move the host driver to the version the AMI pins.** Ubuntu's own archive has
   it — no PPA, no `.run` installer:

   ```sh
   sudo apt install nvidia-driver-590-open   # 590.48.01-0ubuntu0.24.04.5
   ```

   Requires a reboot: the running 595 module cannot be swapped while the display
   is using it. CUDA 12.5/13.0 toolkits already installed are fine on 590.

2. **Put the NVIDIA container toolkit in the agent image** (`docker/Dockerfile.agent`).
   The agent's preflight raises an error-severity check when it detects GPUs and
   finds no `nvidia-ctk`/`nvidia-container-runtime` — "required to pin GPUs into
   worker containers" — and an unschedulable machine registers as `status: failed`
   with `gpu_count: 0`, which is exactly what happened when this was attempted.

   `compute/bootstrap.py` only *configures* the toolkit (`if command -v nvidia-ctk`),
   so a real host gets the binary from the AMI. A containerised agent has to get
   it from its image.

3. **Give the compose agent the card.** The agent detects hardware by parsing
   `nvidia-smi --query-gpu=index,uuid,name`, so it needs the driver injected:

   - `runtime: nvidia` on the `agent` service
   - `NVIDIA_VISIBLE_DEVICES=all`, `NVIDIA_DRIVER_CAPABILITIES=utility,compute`
     (`utility` is what puts `nvidia-smi` in the container)
   - `--max-gpus 1` on the `lazycloud-agent join` line

   Keep them behind `LAZYCLOUD_COMPOSE_AGENT_*` variables defaulting to off, so a
   host with no card is unaffected.

4. **Write the scenario.** One model resident in VRAM serving N concurrent
   invocations at `in_process=True`, against the `self-hosted` pool — the agent is
   the customer's own machine, and `tenant-customer` is the workspace whose
   `default_pool` names it.

## Already done

`GpuType.RTX3090` and its aliases are in `shared/gpu.py`, so every spelling the
driver prints normalises to `RTX3090` before it reaches a capacity record or a
usage row.

`SUPPORTED_GPU_TYPES` and the published rate card are deliberately untouched.
Those say what the platform rents out and resells — what the AWS instance catalog
and the rate card are held to. A card somebody already owns is neither bought nor
resold, and `UsageBillingOwner.SelfHosted` already prices it at zero through the
ordinary pricing path.

## The other way

The paid path is `tests/e2e/external/gpu/`, which runs against prepared GPU
capacity on a deployed platform. It is blocked on IAM rather than on code: the
`lazycloud-connected-aws-control` stack has been deleted, `lazycloud-default-test-operator`
no longer exists, and `user/lazycloud-test` therefore cannot assume anything. The
orphaned `lazycloud-compose-control` role blocks a clean re-bootstrap, since
CloudFormation cannot create a role whose name is taken.

Recovering it: delete that orphan, then

```sh
terraform -chdir=deploy/platform-aws apply \
  --acceptance-operator --trusted-principal-arn arn:aws:iam::<account>:user/lazycloud-test
```

`--profile default` is account root, which `deploy/README.md` reserves for
one-time provisioning the owner directs.
