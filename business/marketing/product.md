# LazyCloud Product Overview

## Deploy Like You Develop

LazyCloud is the deployment platform for developers who don't want to become infrastructure experts just to ship their app.

You already know Docker Compose. You use it every day. Why learn a completely different system just to go to production?

With LazyCloud, your `docker-compose.yaml` IS your deployment config. No translation. No rewriting. No YAML nightmares.

## The Problem

Getting from "works on my machine" to "works in production" shouldn't require:
- Writing infrastructure config files (Terraform, CloudFormation, Pulumi)
- Navigating cloud provider consoles with 200+ services
- Setting up CI/CD pipelines, load balancers, and SSL certificates
- Managing VPCs, security groups, and IAM policies
- Spending weeks on infrastructure instead of your product

You just want to deploy your app and get back to building.

## The Solution

LazyCloud takes your existing Docker Compose file and deploys it to production infrastructure. That's it.

```bash
lazycloud deploy
```

Your app is live. With SSL. With monitoring. With auto-scaling if you want it.

## Core Features

### Instant Deployments
Push your code, get a URL. Your compose file defines your services, and we handle the rest.

### Team Workspaces
Collaborate with your team. Role-based access (Owner, Admin, Member) keeps things organized without getting in the way.

### Real-Time Monitoring
See what's running, what's healthy, what's using resources. No mystery black boxes.

### Custom Domains
Point your domain at your deployment. We handle SSL automatically.

### Auto-Scaling
Your app gets traffic? We scale it. Traffic dies down? We scale back. You don't pay for unused scale-up capacity. Auto-scaling adds or removes replicas based on CPU/memory utilization, working alongside your configured baseline capacity (which you pay for even when idle). Available on Pro (up to 2x) and Scale (up to 5x) tiers.

### Persistent Storage
Attach volumes to your services — they work just like Docker Compose volumes. All volumes support ReadWriteMany (shared between services) natively. Storage is metered at $0.10/GB-month.

### Secrets Management
Environment variables and secrets, stored securely. No committing `.env` files to git.

### Rollbacks
Something broke? Roll back to any previous deployment in seconds.

### Usage-Based Pricing
Pay for what you use. CPU, memory, storage, and build minutes — all metered. Endpoints included. Minimize surprise bills with spending caps, real-time usage alerts, and cost estimation dashboards.

## How It Works

1. **Write** your docker-compose.yaml (you probably already have one)
2. **Run** `lazycloud deploy`
3. **Ship** your product

That's the workflow. No 50-page deployment guides. No certification required.

## Pricing

**Developer** (Free) - Get started without a credit card
- 3 services, unlimited deployments
- Up to 1 CPU, 4GB RAM per service
- Volumes up to 10Gi per service (metered)
- 20Gi ephemeral storage per service
- Perfect for side projects and learning

**Pro** ($19/mo) - For teams shipping real products
- 25 services, unlimited deployments
- Up to 4 CPU, 16GB RAM per service
- Volumes up to 50Gi per service (metered)
- Auto-scaling up to 2x
- Custom domains
- 5 team members

**Scale** ($49/mo) - For growing companies
- Unlimited services and deployments
- Up to 8 CPU, 32GB RAM per service
- Volumes up to 100Gi per service (metered)
- Auto-scaling up to 5x
- Unlimited team members
- Dedicated support

Plus usage: CPU ($0.040/core-hour), Memory ($0.007/GB-hour), Storage ($0.10/GB-month), Build minutes ($0.04/min). Endpoints included.

## vs. The Alternatives

*Comparison as of January 2025. Features and pricing may vary. We recommend verifying current capabilities on each platform's website.*

| | LazyCloud | AWS/GCP/Azure | Heroku | Railway |
|---|---|---|---|---|
| Use existing docker-compose.yaml | Yes | Typically No | No | No |
| No config file rewrites | Yes | Typically No | No | Limited |
| Team collaboration built-in | Yes | Complex (IAM) | Yes | Yes |
| Transparent usage pricing | Yes | Often complex | Limited visibility | Yes |
| Zero learning curve | Yes | Steep curve | Minimal | Minimal |

*Note: "Typically" and "Often" reflect common experiences; individual use cases may vary. AWS/GCP/Azure offer managed container services with varying complexity levels.*
