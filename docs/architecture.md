# Architecture

## Traffic Flow

```mermaid
flowchart LR
    Users --> Cloudflare
    Cloudflare -->|TLS termination| Tunnel[cloudflare-tunnel]

    subgraph EKS[EKS Cluster]
        Tunnel --> NGINX[nginx-ingress]

        subgraph LazyCloud[LazyCloud Apps]
            API[api]
            Web[web]
        end

        subgraph Customer[Customer Apps]
            App1[customer-app-1]
            App2[customer-app-2]
        end

        NGINX --> LazyCloud
        NGINX --> Customer
    end
```

## System Overview

```mermaid
flowchart TB
    subgraph Cloudflare
        DNS[DNS: *.lazycloud.dev]
        TLS[TLS Termination]
    end

    subgraph AWS[AWS us-east-1]
        subgraph EKS[EKS Cluster]
            subgraph Platform
                Tunnel[cloudflare-tunnel]
                NGINX[nginx-ingress]
                ArgoCD[argocd]
                Rollouts[argo-rollouts]
                Karpenter[karpenter]
                ESO[external-secrets]
                Prometheus[prometheus]
                Loki[loki]
            end

            subgraph LazyCloud[LazyCloud Apps]
                API[api]
                Web[web]
            end

            subgraph Customer[Customer Apps]
                App1[customer-app-1]
                App2[customer-app-2]
            end
        end

        SM[Secrets Manager]
    end

    subgraph Depot[Depot.dev]
        Registry[Container Registry]
    end

    DNS --> Tunnel
    Tunnel --> NGINX
    NGINX --> LazyCloud
    NGINX --> Customer
    ESO --> SM
    Customer --> Registry
```

## GitOps Flow

```mermaid
flowchart LR
    Git[GitHub Repo] --> ArgoCD

    subgraph ArgoCD
        Root[root-app.yaml]
        Root --> Platform[platform ApplicationSet]
        Root --> Prod[services-prod ApplicationSet]
        Root --> Staging[services-staging ApplicationSet]
    end

    Platform --> K8s[Kubernetes Resources]
    Prod --> K8s
    Staging --> K8s
```

## Sync Wave Order

```mermaid
flowchart LR
    subgraph Wave1[Wave 1]
        SC[storage-classes]
        R[reloader]
        AR[argo-rollouts]
    end

    subgraph Wave2[Wave 2]
        ES[external-secrets]
    end

    subgraph Wave3[Wave 3]
        NG[nginx-ingress]
    end

    subgraph Wave4[Wave 4]
        CF[cloudflare-tunnel]
        KP[karpenter]
    end

    subgraph Wave5[Wave 5]
        LK[loki]
    end

    subgraph Wave6[Wave 6]
        PM[prometheus-stack]
    end

    subgraph Wave7[Wave 7]
        SVC[services]
    end

    Wave1 --> Wave2 --> Wave3 --> Wave4 --> Wave5 --> Wave6 --> Wave7
```

## Deployment Strategy

LazyCloud services use **blue-green deployments** via [Argo Rollouts](https://argo-rollouts.readthedocs.io/).

```mermaid
flowchart LR
    subgraph Before[Before Promotion]
        Active1[Active Service] --> OldPods[Old Pods v1]
        Preview1[Preview Service] --> NewPods1[New Pods v2]
    end

    subgraph After[After Promotion]
        Active2[Active Service] --> NewPods2[New Pods v2]
        Preview2[Preview Service] --> NewPods2
        OldPods2[Old Pods v1] -.->|scales down| X[removed]
    end

    Before -->|auto-promote when ready| After
```

### How it works

1. **Deploy**: New pods spin up behind a preview service
2. **Verify**: Pods must pass readiness probes
3. **Promote**: Once all replicas are ready, traffic switches atomically
4. **Cleanup**: Old pods scale down after 30 seconds

### Configuration

Services using blue-green (`deploy/services/*/templates/deployment.yaml`):
- `web` - Frontend Next.js app
- `api-platform` - Backend FastAPI

Each has two services:
- **Active service** (`web`, `api-platform-api`) - receives production traffic
- **Preview service** (`web-preview`, `api-platform-api-preview`) - receives new version during rollout

### Manual operations

Check rollout status:
```bash
kubectl argo rollouts get rollout web -n lazycloud-prod
kubectl argo rollouts get rollout api-platform-api -n lazycloud-prod
```

Manually promote (if `autoPromotionEnabled: false`):
```bash
kubectl argo rollouts promote web -n lazycloud-prod
```

Abort and rollback:
```bash
kubectl argo rollouts abort web -n lazycloud-prod
kubectl argo rollouts undo web -n lazycloud-prod
```

Dashboard (if enabled):
```bash
kubectl argo rollouts dashboard -n argo-rollouts
```
