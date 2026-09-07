# The tunnel's identity, read from the module that owns it.
#
# `deploy/cloudflare` creates the tunnel and holds its credentials; this
# deployment needs them in Secrets Manager so the host can write the file
# cloudflared reads. Copying that value by hand would work once and be wrong the
# next time the tunnel is replaced, so it is read rather than pasted.
#
# This is the reason the modules stay separate rather than merging into one
# stack: they change at different rates and need different credentials, and
# sharing a value across them is a solved problem.
data "terraform_remote_state" "cloudflare" {
  backend = "s3"

  config = merge(local.terraform_backend_config, { key = var.cloudflare_state_key })
}
