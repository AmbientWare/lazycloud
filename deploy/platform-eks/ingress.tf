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

  config = {
    bucket = var.state_bucket
    key    = var.cloudflare_state_key
    region = var.region
  }
}

resource "aws_secretsmanager_secret_version" "tunnel_credentials" {
  secret_id = aws_secretsmanager_secret.runtime["cloudflare-tunnel-credentials"].id

  # Written verbatim: this is the whole of the tunnel's identity, and cloudflared
  # reads the file as-is.
  secret_string = data.terraform_remote_state.cloudflare.outputs.tunnel_credentials
}
