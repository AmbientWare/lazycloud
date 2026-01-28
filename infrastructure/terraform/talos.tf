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

  # Control plane machine config patch
  controlplane_patches = [for i in range(var.control_plane_count) : yamlencode({
    machine = {
      install = {
        # Schematic includes: siderolabs/gvisor
        image = "factory.talos.dev/installer/d9ff89777e246792e7642abd3220a616afb4e49822382e4213a2e528ab826fe5:${var.talos_version}"
        extraKernelArgs = ["ipv6.disable=1"]
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
          enabled                    = true
          allowedRoles               = ["os:reader"]
          allowedKubernetesNamespaces = ["kube-system"]
        }
        hostDNS = {
          enabled              = true
          forwardKubeDNSToHost = true
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
        dnsDomain = "cluster.local"
        podSubnets     = [var.pod_ipv4_cidr]
        serviceSubnets = [var.service_ipv4_cidr]
        cni = { name = "none" }
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

  # Worker machine config patch
  worker_patches = [for i in range(var.worker_count) : yamlencode({
    machine = {
      install = {
        # Schematic includes: siderolabs/gvisor
        image = "factory.talos.dev/installer/d9ff89777e246792e7642abd3220a616afb4e49822382e4213a2e528ab826fe5:${var.talos_version}"
        extraKernelArgs = ["ipv6.disable=1"]
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
          forwardKubeDNSToHost = true
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
        dnsDomain = "cluster.local"
        podSubnets     = [var.pod_ipv4_cidr]
        serviceSubnets = [var.service_ipv4_cidr]
        cni = { name = "none" }
      }
    }
  })]
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

data "talos_machine_configuration" "worker" {
  count              = var.worker_count
  talos_version      = var.talos_version
  cluster_name       = var.cluster_name
  cluster_endpoint   = local.cluster_endpoint
  kubernetes_version = var.kubernetes_version
  machine_type       = "worker"
  machine_secrets    = talos_machine_secrets.this.machine_secrets
  config_patches     = [local.worker_patches[count.index]]
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
