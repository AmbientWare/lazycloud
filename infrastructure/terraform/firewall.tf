# -----------------------------------------------------------------------------
# Firewall
# -----------------------------------------------------------------------------

data "http" "my_ip" {
  url = "https://ipv4.icanhazip.com"
  retry {
    attempts     = 3
    min_delay_ms = 1000
    max_delay_ms = 2000
  }
}

locals {
  my_ip = "${chomp(data.http.my_ip.response_body)}/32"
}

resource "hcloud_firewall" "this" {
  name = var.cluster_name

  rule {
    description = "Allow Kube API from current IP"
    direction   = "in"
    protocol    = "tcp"
    port        = "6443"
    source_ips  = [local.my_ip]
  }

  rule {
    description = "Allow Talos API from current IP"
    direction   = "in"
    protocol    = "tcp"
    port        = "50000"
    source_ips  = [local.my_ip]
  }

  labels = {
    cluster = var.cluster_name
  }
}
