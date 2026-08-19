# Platform AWS

Terraform for the infrastructure this platform runs on. `README.md` is the
operator runbook and states what this module deliberately does not own.

- The boundary is ownership, not provider. Anything running in an account we hold
  credentials for belongs here; anything running in a customer's account stays
  CloudFormation, because we cannot run Terraform there.
- Never declare a secret's value. This module declares the container and the
  access to it. A value in Terraform is a value in the state file.
- Never declare the fleet network or the connection role. Both come from the
  account connection stack the control plane generates, and a copy here would
  drift from the generator that owns it.
- A resource that must outlive its manager does not belong in a tool that
  converges to declared state. `control-stack.yaml` is the standing example.
