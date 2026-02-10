# -----------------------------------------------------------------------------
# Talos machine secrets & configuration
# -----------------------------------------------------------------------------

resource "talos_machine_secrets" "this" {
  talos_version = var.talos_version
}

locals {
  cluster_endpoint = "https://${local.cp_vip_ipv4}:6443"

  cert_sans = distinct(concat(
    [local.cp_vip_ipv4],
    local.cp_public_ipv4,
    local.cp_private_ipv4,
  ))

  # Talos factory image URL
  talos_install_image = "factory.talos.dev/installer/${var.talos_schematic_id}:${var.talos_version}"

  # Control plane machine config patch
  controlplane_patches = [for i in range(var.control_plane_count) : yamlencode({
    machine = {
      install = {
        image = local.talos_install_image
      }
      certSANs = local.cert_sans
      kubelet = {
        extraArgs = {
          "cloud-provider"             = "external"
          "rotate-server-certificates" = true
        }
        nodeIP = {
          validSubnets = [var.node_ipv4_cidr]
        }
      }
      network = {
        nameservers = ["1.1.1.1", "8.8.8.8"]
        interfaces = [
          {
            interface = "eth0"
            dhcp      = true
            vip = {
              ip = local.cp_vip_ipv4
              hcloud = {
                apiToken = var.hcloud_token
              }
            }
          },
          { interface = "eth1", dhcp = true },
        ]
        kubespan = { enabled = false }
      }
      kernel = {
        modules = [{ name = "nbd" }]
      }
      sysctls = {
        "net.core.somaxconn"          = "65535"
        "net.core.netdev_max_backlog" = "4096"
        "user.max_user_namespaces"    = "11255"
      }
      features = {
        kubernetesTalosAPIAccess = {
          enabled                     = true
          allowedRoles                = ["os:reader"]
          allowedKubernetesNamespaces = ["kube-system"]
        }
        hostDNS = {
          enabled              = true
          forwardKubeDNSToHost = false
          resolveMemberNames   = true
        }
      }
      time = {
        servers = [
          "ntp1.hetzner.de",
          "ntp2.hetzner.com",
          "ntp3.hetzner.net",
          "time.cloudflare.com",
        ]
      }
    }
    cluster = {
      allowSchedulingOnControlPlanes = false
      network = {
        dnsDomain      = "cluster.local"
        podSubnets     = [var.pod_ipv4_cidr]
        serviceSubnets = [var.service_ipv4_cidr]
        cni            = { name = "none" }
      }
      proxy = { disabled = true }
      apiServer = {
        certSANs = local.cert_sans
      }
      controllerManager = {
        extraArgs = {
          "cloud-provider"           = "external"
          "node-cidr-mask-size-ipv4" = split("/", var.node_ipv4_cidr)[1]
          "bind-address"             = "0.0.0.0"
        }
      }
      etcd = {
        advertisedSubnets = [var.node_ipv4_cidr]
        extraArgs = {
          "listen-metrics-urls" = "http://0.0.0.0:2381"
        }
      }
      scheduler = {
        extraArgs = { "bind-address" = "0.0.0.0" }
      }
      inlineManifests = [
        {
          name = "hcloud-secret"
          contents = yamlencode({
            apiVersion = "v1"
            kind       = "Secret"
            type       = "Opaque"
            metadata = {
              name      = "hcloud"
              namespace = "kube-system"
            }
            data = {
              network = base64encode(hcloud_network.this.id)
              token   = base64encode(var.hcloud_token)
            }
          })
        }
      ]
      externalCloudProvider = {
        enabled = true
        manifests = [
          "https://raw.githubusercontent.com/siderolabs/talos-cloud-controller-manager/v1.6.0/docs/deploy/cloud-controller-manager-daemonset.yml"
        ]
      }
    }
  })]

  # Platform machine config patch (no gVisor, label: instance-class=platform)
  platform_patches = [for i in range(var.platform_count) : yamlencode({
    machine = {
      install = {
        image = local.talos_install_image
      }
      certSANs = local.cert_sans
      kubelet = {
        extraArgs = {
          "cloud-provider"             = "external"
          "rotate-server-certificates" = true
        }
        nodeIP = {
          validSubnets = [var.node_ipv4_cidr]
        }
      }
      nodeLabels = {
        "instance-class" = "platform"
      }
      network = {
        nameservers = ["1.1.1.1", "8.8.8.8"]
        interfaces = [
          { interface = "eth0", dhcp = true },
          { interface = "eth1", dhcp = true },
        ]
        kubespan = { enabled = false }
      }
      kernel = {
        modules = [{ name = "nbd" }]
      }
      sysctls = {
        "net.core.somaxconn"          = "65535"
        "net.core.netdev_max_backlog" = "4096"
        "user.max_user_namespaces"    = "11255"
      }
      features = {
        hostDNS = {
          enabled              = true
          forwardKubeDNSToHost = false
          resolveMemberNames   = true
        }
      }
      time = {
        servers = [
          "ntp1.hetzner.de",
          "ntp2.hetzner.com",
          "ntp3.hetzner.net",
          "time.cloudflare.com",
        ]
      }
    }
    cluster = {
      network = {
        dnsDomain      = "cluster.local"
        podSubnets     = [var.pod_ipv4_cidr]
        serviceSubnets = [var.service_ipv4_cidr]
        cni            = { name = "none" }
      }
    }
  })]

  # Sandbox worker machine config patch (shared by static workers + autoscaler template)
  sandbox_patch = yamlencode({
    machine = {
      install = {
        image = local.talos_install_image
      }
      certSANs = local.cert_sans
      kubelet = {
        extraArgs = {
          "cloud-provider"             = "external"
          "rotate-server-certificates" = true
          "register-with-taints"       = "instance-class=sandbox:NoSchedule"
        }
        nodeIP = {
          validSubnets = [var.node_ipv4_cidr]
        }
      }
      nodeLabels = {
        "instance-class" = "sandbox"
        "runtime"        = "gvisor"
      }
      network = {
        nameservers = ["1.1.1.1", "8.8.8.8"]
        interfaces = [
          { interface = "eth0", dhcp = true },
          { interface = "eth1", dhcp = true },
        ]
        kubespan = { enabled = false }
      }
      kernel = {
        modules = [{ name = "nbd" }]
      }
      sysctls = {
        "net.core.somaxconn"          = "65535"
        "net.core.netdev_max_backlog" = "4096"
        "user.max_user_namespaces"    = "11255"
      }
      features = {
        hostDNS = {
          enabled              = true
          forwardKubeDNSToHost = false
          resolveMemberNames   = true
        }
      }
      time = {
        servers = [
          "ntp1.hetzner.de",
          "ntp2.hetzner.com",
          "ntp3.hetzner.net",
          "time.cloudflare.com",
        ]
      }
    }
    cluster = {
      network = {
        dnsDomain      = "cluster.local"
        podSubnets     = [var.pod_ipv4_cidr]
        serviceSubnets = [var.service_ipv4_cidr]
        cni            = { name = "none" }
      }
    }
  })

}

# -----------------------------------------------------------------------------
# Machine configurations
# -----------------------------------------------------------------------------

data "talos_machine_configuration" "control_plane" {
  count              = var.control_plane_count
  talos_version      = var.talos_version
  cluster_name       = var.cluster_name
  cluster_endpoint   = local.cluster_endpoint
  kubernetes_version = var.kubernetes_version
  machine_type       = "controlplane"
  machine_secrets    = talos_machine_secrets.this.machine_secrets
  config_patches     = [local.controlplane_patches[count.index]]
  docs               = false
  examples           = false
}

data "talos_machine_configuration" "platform" {
  count              = var.platform_count
  talos_version      = var.talos_version
  cluster_name       = var.cluster_name
  cluster_endpoint   = local.cluster_endpoint
  kubernetes_version = var.kubernetes_version
  machine_type       = "worker"
  machine_secrets    = talos_machine_secrets.this.machine_secrets
  config_patches     = [local.platform_patches[count.index]]
  docs               = false
  examples           = false
}

# Sandbox template for cluster autoscaler
data "talos_machine_configuration" "sandbox_autoscaler" {
  talos_version      = var.talos_version
  cluster_name       = var.cluster_name
  cluster_endpoint   = local.cluster_endpoint
  kubernetes_version = var.kubernetes_version
  machine_type       = "worker"
  machine_secrets    = talos_machine_secrets.this.machine_secrets
  config_patches     = [local.sandbox_patch]
  docs               = false
  examples           = false
}

# -----------------------------------------------------------------------------
# Bootstrap & kubeconfig
# -----------------------------------------------------------------------------

resource "talos_machine_bootstrap" "this" {
  client_configuration = talos_machine_secrets.this.client_configuration
  endpoint             = local.cp_public_ipv4[0]
  node                 = local.cp_public_ipv4[0]
  depends_on           = [hcloud_server.control_plane]
}

data "talos_client_configuration" "this" {
  cluster_name         = var.cluster_name
  client_configuration = talos_machine_secrets.this.client_configuration
  endpoints            = local.cp_public_ipv4
}

resource "talos_cluster_kubeconfig" "this" {
  client_configuration = talos_machine_secrets.this.client_configuration
  node                 = local.cp_public_ipv4[0]
  depends_on           = [talos_machine_bootstrap.this]
}

# -----------------------------------------------------------------------------
# Wait for cluster to be healthy before proceeding with Helm/K8s resources
# -----------------------------------------------------------------------------

resource "null_resource" "wait_for_cluster_health" {
  provisioner "local-exec" {
    command = <<-EOT
      echo "Waiting for Kubernetes API to be ready (CNI not yet installed, nodes will be NotReady)..."
      for i in $(seq 1 60); do
        # Check if API server responds and can list nodes
        # Nodes won't be Ready until CNI is installed, but API must be accessible for Helm
        if kubectl --kubeconfig <(echo "$KUBECONFIG") get --raw /readyz >/dev/null 2>&1; then
          echo "Kubernetes API is ready!"
          kubectl --kubeconfig <(echo "$KUBECONFIG") get nodes
          exit 0
        fi
        echo "Attempt $i/60: API not ready yet, waiting 10s..."
        sleep 10
      done
      echo "Timed out waiting for Kubernetes API"
      exit 1
    EOT
    environment = {
      KUBECONFIG = talos_cluster_kubeconfig.this.kubeconfig_raw
    }
    interpreter = ["bash", "-c"]
  }

  depends_on = [talos_machine_bootstrap.this]
}
