# LazyCloud Pricing Strategy

## Overview

LazyCloud uses usage-based pricing on Hetzner Cloud infrastructure. Our pricing is designed to be:
- **Transparent**: You see exactly what you're paying for
- **Competitive**: Positioned between Modal (higher) and Fly.io (lower)
- **Predictable**: No surprise bills, real-time cost tracking

---

## Pricing Model

We charge for four resource types, all billed by usage:

| Resource | Unit | Price |
|----------|------|-------|
| CPU | per core-hour | $0.040 |
| Memory | per GB-hour | $0.007 |
| Storage | per GB-month | $0.10 |
| Build Minutes | per minute | $0.04 |

### Included in Tier (Not Metered)

**Public Endpoints:** Included for all tiers. Custom domains require Pro tier or above. Endpoints are essentially ingress rules with automatic SSL via Cloudflare — minimal marginal cost.

**Ephemeral Storage:** Every service gets up to 20Gi of ephemeral storage from node local disk (for temp files, caches, etc.). This is not persistent and is not billed.

---

## Cost Basis: Underlying Infrastructure

### Hetzner Cloud Compute (Ashburn, VA)

**Dedicated vCPU Instances (CCX series, single pool):**

| Instance | vCPU | Memory | Disk | Monthly Price |
|----------|------|--------|------|---------------|
| CCX13 | 2 | 8 GB | 80 GB | $13.49 |
| CCX23 | 4 | 16 GB | 160 GB | $26.49 |
| CCX33 | 8 | 32 GB | 240 GB | $53.49 |

*Source: [Hetzner Cloud Pricing](https://www.hetzner.com/cloud)*

**Worker node: CCX33** ($53.49/mo, 8 dedicated vCPU, 32GB RAM, 240GB disk). All tiers packed onto a single node pool. Dedicated vCPU ensures consistent performance for customer workloads. Local disk used for OS, container images, and ephemeral storage only — no persistent storage on nodes.

**Our compute markup (Hetzner CCX33):**
- Hetzner effective CPU: ~$0.009/vCPU-hour ($53.49 / 730h / 8 vCPU)
- LazyCloud CPU: $0.040/core-hour → **~340% markup**
- Hetzner effective memory: ~$0.002/GB-hour ($53.49 / 730h / 32GB)
- LazyCloud memory: $0.007/GB-hour → **~250% markup**

Note: Dedicated vCPU eliminates noisy-neighbor performance variance. Compute margins provide buffer for platform overhead (system pods, scheduling fragmentation, ~25% node utilization overhead).

### Hetzner Storage (JuiceFS Cloud + Hetzner Object Storage)

Persistent storage is decoupled from compute. All volumes use JuiceFS Cloud (managed POSIX filesystem) backed by Hetzner Object Storage for data.

| Component | Cost |
|-----------|------|
| JuiceFS Cloud (managed metadata) | $0.02/GiB-month |
| Hetzner Object Storage (data) | $0.006/GB-month |
| **Total storage cost** | **~$0.026/GB-month** |

**Our storage markup:**
- Infrastructure cost: ~$0.026/GB-month
- LazyCloud price: $0.10/GB-month → **~74% margin**

Storage features:
- POSIX filesystem — transparent to customer apps
- ReadWriteMany (RWX) natively supported — shared volumes work out of the box
- No per-node volume attachment limits (unlike Hetzner block volumes)
- Scales independently of compute

### Depot Build Service

We use [Depot](https://depot.dev) for remote Docker image builds.

| Build Type | Depot Price | Specs |
|------------|-------------|-------|
| Default | **$0.04/minute** | 16 vCPU, 32 GB RAM |

*Source: [Depot Pricing](https://depot.dev/pricing)*

**Our build markup:**
- Depot default: $0.04/minute
- LazyCloud: $0.04/minute → **0% markup (pass-through)**

### Cloudflare for SaaS (Custom Domains)

We use [Cloudflare for SaaS](https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/) for custom domain management. Endpoints are included (not metered) — cost absorbed into compute margins.

| Component | Cloudflare Price |
|-----------|------------------|
| First 100 hostnames | Included in plan |
| Additional hostnames | **$0.10/hostname/month** |
| SSL certificates | Included (automatic) |
| DDoS protection | Included |

*Source: [Cloudflare for SaaS Plans](https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/plans/)*

---

## Competitive Positioning

LazyCloud is priced **between Modal and Fly.io**, offering a balance of simplicity and cost-effectiveness.

### Compute Comparison

| Platform | CPU Price | Memory Price | Model |
|----------|-----------|--------------|-------|
| **Modal** | $0.047/core-hr | $0.008/GB-hr | Per-second billing |
| **LazyCloud** | $0.040/core-hr | $0.007/GB-hr | Per-second billing |
| **Railway** | $0.028/vCPU-hr | $0.014/GB-hr | Per-second billing |
| **Fly.io** | ~$0.02-0.04/vCPU-hr | ~$0.007/GB-hr | Per-second billing |
| **Render** | Fixed tiers ($7-450/mo) | Included | Monthly tiers |
| **Heroku** | Fixed dynos ($7-500/mo) | Included | Monthly tiers |

### Storage Comparison

| Platform | Storage Price | Shared Storage (RWX) |
|----------|-------------|----------------------|
| **LazyCloud** | $0.10/GB-month | Yes (all volumes shared by default) |
| **Fly.io** | $0.15/GB-month | No |
| **Railway** | ~$0.16/GB-month | No |
| **Render** | $0.25/GB-month | No |
| **AWS Direct** | $0.08/GB-month (EBS) | $0.30/GB-month (EFS) |

### Build Minutes Comparison

| Platform | Build Price | Notes |
|----------|-------------|-------|
| **LazyCloud** | $0.04/minute | Via Depot, 16 vCPU |
| **Depot Direct** | $0.04/minute | Same infrastructure |
| **Railway** | Included | Limited minutes per plan |
| **Render** | Included | Limited, then throttled |
| **GitHub Actions** | $0.008/minute | 2 vCPU Linux runners |

### Where We Win

1. **vs. Modal**: 15% cheaper on CPU, 12% cheaper on memory, simpler deployment model
2. **vs. Fly.io**: Easier Docker Compose workflow, comparable pricing, 33% cheaper storage ($0.10 vs $0.15), native shared volumes
3. **vs. Railway**: Native compose support, 38% cheaper storage ($0.10 vs $0.16), 50% cheaper memory
4. **vs. Render/Heroku**: Usage-based (pay for what you use) vs. fixed tiers (pay for capacity), 60% cheaper storage
5. **vs. AWS/GCP/Azure**: No infrastructure management, no cloud expertise needed

---

## Competitor Deep Dive

### Modal
- **Focus**: Serverless GPU/ML workloads
- **Pricing model**: Per-second billing, very granular
- **CPU**: $0.0000131/core/sec = **$0.047/core-hour**
- **Memory**: $0.00000222/GiB/sec = **$0.008/GiB-hour**
- **GPUs**: From $0.59/hr (T4) to $6.25/hr (B200)
- **Strengths**: Excellent for bursty ML workloads, scale-to-zero
- **Weaknesses**: Python-centric, requires code changes, no compose support

### Fly.io
- **Focus**: Edge deployment, global distribution
- **Pricing model**: Per-second billing, preset machine sizes
- **Compute**: $0.0028-1.41/hr depending on machine size
- **Memory**: ~$0.007/GB-hour (additional RAM)
- **Storage**: $0.15/GB-month (local NVMe volumes, no shared filesystem)
- **GPUs**: $1.25-3.50/hr
- **Strengths**: Global edge network, good CLI, Postgres integration
- **Weaknesses**: Custom config format (fly.toml), no shared filesystem

### Railway
- **Focus**: Git-based deployments, developer experience
- **Pricing model**: Per-second usage + monthly plan fee
- **CPU**: $0.00000772/vCPU/sec = **$0.028/vCPU-hour**
- **Memory**: $0.00000386/GB/sec = **$0.014/GB-hour**
- **Storage**: ~$0.16/GB-month
- **Plans**: Free ($5 credits), Hobby ($5/mo), Pro ($20/mo)
- **Strengths**: Great DX, GitHub integration, nixpacks auto-detection
- **Weaknesses**: No native Docker Compose, Git-push centric

### Render
- **Focus**: Heroku alternative, simple deployments
- **Pricing model**: Fixed monthly tiers
- **Tiers**: Free → Starter ($7) → Standard ($25) → Pro ($85) → Pro Ultra ($450)
- **Storage**: $0.25/GB-month
- **Strengths**: Simple pricing, good free tier, managed databases
- **Weaknesses**: Fixed tiers waste resources, no usage-based option

### Heroku
- **Focus**: Original PaaS, enterprise features
- **Pricing model**: Fixed dyno tiers
- **Tiers**: Basic ($7/mo) → Standard ($25-50/mo) → Performance ($250-500/mo)
- **Strengths**: Mature ecosystem, enterprise features, add-on marketplace
- **Weaknesses**: Expensive at scale, aging platform, Salesforce ownership

---

## Pricing Strategy Rationale

### Why Usage-Based?

1. **Fairness**: Customers pay for actual consumption, not reserved capacity
2. **Transparency**: Easy to understand and predict costs
3. **Alignment**: Our costs scale with customer usage, margins stay consistent
4. **Competition**: Industry moving toward usage-based (Railway, Fly.io, Modal)

### Why This Price Point?

**CPU at $0.040/core-hour:**
- ~340% over Hetzner CCX33 (~$0.009/vCPU-hr) — healthy margin covers platform overhead and node utilization losses (~25%)
- 15% below Modal ($0.047)
- Higher than Railway ($0.028) — we offer simpler compose workflow + dedicated vCPU

**Memory at $0.007/GB-hour:**
- ~250% over Hetzner CCX33 (~$0.002/GB-hr) — strong margin (dedicated instances have 2:1 RAM:CPU ratio)
- 12% below Modal ($0.008)
- 50% below Railway ($0.014) — major differentiator

**Storage at $0.10/GB-month:**
- ~74% margin over JuiceFS Cloud + Hetzner Object Storage (~$0.026/GB-month)
- 33% cheaper than Fly.io ($0.15/GB-month)
- 38% cheaper than Railway ($0.16/GB-month)
- 60% cheaper than Render ($0.25/GB-month)
- Native ReadWriteMany — no competitor offers shared volumes at any price

**Build Minutes at $0.04/minute:**
- Pass-through from Depot (0% markup)
- Competitive with premium build services
- Much faster than GitHub Actions (16 vCPU vs 2 vCPU)

**Public Endpoints (included):**
- Endpoints included for all tiers, custom domains require Pro+
- Cloudflare cost (~$0.10/hostname/month) absorbed into compute margins
- Simplifies billing — customers don't think about endpoint costs

### Margin Analysis

**Margin breakdown:**

| Component | Our Cost | LazyCloud Price | Gross Margin |
|-----------|----------|-----------------|--------------|
| 1 core-hour CPU | ~$0.009 (CCX33) | $0.040 | ~77% |
| 1 GB-hour memory | ~$0.002 (CCX33) | $0.007 | ~67% |
| 1 GB-month storage | ~$0.026 (JuiceFS+ObjStore) | $0.10 | ~74% |
| 1 build minute | $0.04 (Depot) | $0.04 | 0% |
| Endpoints | ~$0.10/mo (Cloudflare) | Included | N/A (absorbed) |

**Target margin**: 50-70% gross margin, covering:
- Kubernetes system pods and scheduling overhead (~25% node utilization loss)
- Monitoring and observability (Prometheus, Grafana)
- Platform development and support
- Billing and payment processing (~3%)
- Infrastructure redundancy and backups

---

## Subscription Tiers

Tiers are feature gates that set resource limits. All compute, storage, and builds are metered on top. Services (containers defined in docker-compose.yaml) are the primary unit of scale.

| | **Developer** | **Pro** | **Scale** |
|---|---|---|---|
| **Monthly Fee** | $0 | $19 | $49 |
| **Services** | 3 | 25 | Unlimited |
| **Deployments** | Unlimited | Unlimited | Unlimited |
| **Min CPU/service** | 0.25 cores | 0.25 cores | 0.25 cores |
| **Min Memory/service** | 0.5 GB | 0.5 GB | 0.5 GB |
| **Max CPU/service** | 1 core | 4 cores | 8 cores |
| **Max Memory/service** | 4 GB | 16 GB | 32 GB |
| **Max Replicas/service** | 1 | 2 | 5 |
| **Max Volume/service** | 10 Gi | 50 Gi | 100 Gi |
| **Ephemeral/service** | 20 Gi | 20 Gi | 20 Gi |
| **Custom Domains** | No | Yes | Yes |
| **Team Members** | 1 | 5 | Unlimited |
| **Auto-Scaling** | No | Up to 2x | Up to 5x |
| **Support** | Community | Priority | Dedicated |

**Tier details:**
- **Developer**: Free tier for hobbyists and side projects. Limited to 3 services total across all deployments.
- **Pro**: For teams shipping production workloads. 25 services, team collaboration, auto-scaling (up to 2x), custom domains.
- **Scale**: For growing companies. Unlimited services, high resource limits, dedicated support.

Usage (CPU, memory, storage, builds) is billed on top of the subscription fee. Endpoints are included — not metered. The subscription unlocks features and sets resource limits; all resource usage is pay-as-you-go.

### Compute Architecture

Two node pools:
- **Platform pool**: CPX31 shared instances (4 vCPU, 8GB RAM, $18.59/mo) — fixed nodes running LazyCloud infrastructure (API, monitoring, ingress)
- **Sandbox pool**: CCX33 dedicated instances (8 vCPU, 32GB RAM, $53.49/mo) — autoscaled nodes running customer workloads with gVisor isolation

Customer workloads from all tiers are packed onto sandbox nodes for efficient utilization. Dedicated vCPU ensures consistent performance. The cluster autoscaler scales sandbox nodes (2-20) based on demand. Platform nodes are fixed and not billed to customers.

### Storage Architecture

All persistent volumes use JuiceFS Cloud (POSIX filesystem) backed by Hetzner Object Storage:
- No per-node volume attachment limits (JuiceFS mounts via CSI driver, not block devices)
- All volumes are ReadWriteMany by default — shared volumes work like Docker Compose
- Storage scales independently of compute (no node disk capacity planning)
- POSIX-compliant — transparent to customer applications

---

## Example Cost Scenarios

### Developer Tier - Hobbyist
- 2 services (0.25 CPU, 0.5GB RAM each)
- 1 volume (2Gi)
- 4 builds/month (~5 min each = 20 min/month)

**Monthly cost:**
- CPU: 0.5 cores × 730 hrs × $0.040 = **$14.60**
- Memory: 1.0 GB × 730 hrs × $0.007 = **$5.11**
- Storage: 2 GB × $0.10 = **$0.20**
- Builds: 20 min × $0.04 = **$0.80**
- Endpoints: **included**
- **Total: ~$21/month** (Developer tier - $0 subscription)

### Developer Tier - Active Side Project
- 3 services (0.5 CPU, 1GB RAM each)
- 3 volumes (5Gi each = 15GB)
- 20 builds/month (~8 min each = 160 min/month)

**Monthly cost:**
- CPU: 1.5 cores × 730 hrs × $0.040 = **$43.80**
- Memory: 3 GB × 730 hrs × $0.007 = **$15.33**
- Storage: 15 GB × $0.10 = **$1.50**
- Builds: 160 min × $0.04 = **$6.40**
- Endpoints: **included**
- **Total: ~$67/month** (Developer tier - maxing out 3 services)

### Pro Tier - Startup
- 15 services (0.5 CPU, 1GB RAM each)
- 10 volumes (10Gi each = 100GB)
- 40 builds/month (~8 min each = 320 min/month)

**Monthly cost:**
- CPU: 7.5 cores × 730 hrs × $0.040 = **$219.00**
- Memory: 15 GB × 730 hrs × $0.007 = **$76.65**
- Storage: 100 GB × $0.10 = **$10.00**
- Builds: 320 min × $0.04 = **$12.80**
- Endpoints: **included**
- Pro subscription: **$19**
- **Total: ~$337/month**

### Scale Tier - Growing Company
- 40 services (0.5 CPU, 1GB RAM each)
- 25 volumes (20Gi each = 500GB)
- 100 builds/month (~10 min each = 1000 min/month)

**Monthly cost:**
- CPU: 20 cores × 730 hrs × $0.040 = **$584.00**
- Memory: 40 GB × 730 hrs × $0.007 = **$204.40**
- Storage: 500 GB × $0.10 = **$50.00**
- Builds: 1000 min × $0.04 = **$40.00**
- Endpoints: **included**
- Scale subscription: **$49**
- **Total: ~$927/month**

---

## Future Pricing Considerations

1. **GPU Support**: Price at 1.5-2x provider on-demand (competitive with Modal)
2. **Reserved Capacity**: Offer 20-40% discount for committed usage
3. **Volume Discounts**: Tiered pricing for high-usage customers
4. **Regional Pricing**: Adjust for non-US regions based on underlying costs
5. **Build Caching**: Potential separate pricing for persistent build cache
6. **Burstable Tier**: Offer cheaper CPX shared instances for cost-sensitive workloads that don't need guaranteed CPU

---

## References

- [Hetzner Cloud Pricing](https://www.hetzner.com/cloud)
- [JuiceFS Cloud Pricing](https://juicefs.com/docs/cloud/pricing/)
- [Depot Pricing](https://depot.dev/pricing)
- [Cloudflare for SaaS Plans](https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/plans/)
- [Modal Pricing](https://modal.com/pricing)
- [Fly.io Pricing](https://fly.io/docs/about/pricing/)
- [Railway Pricing](https://railway.com/pricing)
- [Render Pricing](https://render.com/pricing)
