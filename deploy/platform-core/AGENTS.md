# Platform core

Terraform for the one cluster every deployment runs on: its network, the EKS
control plane, the node identity, the image repositories, the OIDC provider,
the storage class, and Argo CD. Applied once. Everything a single deployment
owns is in `deploy/platform-deployment`, applied once per deployment.

- The boundary is "one per cluster" against "one per deployment", and it is
  drawn by what would collide. A second deployment needs its own database,
  Redis, buckets, secret documents, roles and namespace; it does not need a
  second cluster, a second registry or a second Argo. Anything two deployments
  would each have to own is in the other module.
- This is greenfield and stays greenfield. An apply produces the cluster; it
  does not adopt one. Do not add import blocks or reconciliation against
  hand-built resources.
- Argo's root Application reads `main`, not a deployment branch. What it finds
  in `deploy/argocd/apps` is the list of things this cluster runs: the
  operators every deployment shares and one Application per deployment, each
  naming its own branch. Which deployments exist is a fact about the cluster;
  what one runs is a fact about that deployment and lives on its branch.
- Image repositories are shared and tagged by commit. An image is a fact about
  a commit, not about where it runs, so promoting a commit from staging to prod
  is a values file and no push. The deploy role of every deployment may push
  here and every control plane may pull; the ARNs are outputs.
- The OIDC provider exists for one identity per deployment, the secret reader.
  Workload pods hold their AWS identity through Pod Identity, which the
  deployment module associates; the External Secrets store cannot, because a
  shared operator reconciles it and the only per-store identity it can present
  is a service account token exchanged through this federation.
- The storage class is here because it is cluster-scoped. Two deployments
  syncing a chart that declared it would each claim it, and Argo would prune
  it from under the other.
- Auto Mode provisions everything, including the control plane. No node group
  is declared here and no workload names a node: Karpenter sizes from what the
  pods request, so a hand-declared pool would be choosing hardware on its
  behalf and paying for it whether or not anything lands there. Every
  deployment's pods land on the same nodes, so a request understated in one
  chart is charged to the other deployment's pods.
- `cluster_api_cidrs` is this module's alone. The Kubernetes and Helm providers
  reach the API from wherever the apply runs, so that address is listed here. A
  deployment apply never reaches the API and needs no listing.
