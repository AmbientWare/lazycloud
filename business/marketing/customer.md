# LazyCloud Customer Profile

## Who We're Building For

LazyCloud is for developers who want to ship, not configure.

### Ideal Customer Profile (ICP)

**Primary:**
- Solo developers and indie hackers building SaaS products
- Startup engineering teams (2-10 developers)
- Small agencies deploying client projects

**Characteristics:**
- Already using Docker and Docker Compose for local development
- Technically capable but don't want to become infrastructure specialists
- Value their time over saving $20/month on hosting
- Building web apps, APIs, or services (not enterprise legacy systems)

**Anti-patterns (not our customer):**
- Enterprise teams with dedicated DevOps/Platform teams
- Companies requiring on-premise deployments
- Developers who enjoy writing infrastructure config files

---

## Customer Personas

### 1. The Indie Hacker - "Alex"

**Background:**
- Building a SaaS product solo or with 1-2 others
- Technical founder, probably a full-stack developer
- Has a day job or recently quit to go full-time

**Goals:**
- Ship the MVP fast and iterate based on feedback
- Keep costs low while validating the idea
- Not get distracted by infrastructure

**Pain Points:**
- Every hour on DevOps is an hour not spent on the product
- Heroku is expensive, AWS is complex
- Just wants something that works without thinking about it

**Why LazyCloud:**
"I already have a docker-compose.yaml. Why can't I just deploy that?"

---

### 2. The Startup Tech Lead - "Jordan"

**Background:**
- Engineering lead at a seed/Series A startup
- Team of 3-8 developers
- Moving fast, shipping features weekly

**Goals:**
- Keep the team productive without dedicated DevOps
- Have visibility into deployments and costs
- Enable team members to deploy without hand-holding

**Pain Points:**
- Doesn't have time to set up and maintain cloud infrastructure
- Needs team collaboration without complexity
- Wants to see what things cost before the bill arrives

**Why LazyCloud:**
"We need something the whole team can use without a 3-day onboarding."

---

### 3. The Agency Developer - "Sam"

**Background:**
- Developer at a small agency or freelancer
- Deploys multiple client projects
- Needs to hand off projects that clients can manage

**Goals:**
- Deploy client projects quickly
- Keep client costs predictable
- Minimal ongoing maintenance

**Pain Points:**
- Every client is a different hosting setup
- Clients ask "why is this so expensive?"
- Doesn't want to be on-call for infrastructure issues

**Why LazyCloud:**
"I need one workflow for all my projects, and clients need to understand the bill."

---

## Pain Points We Solve

1. **"I don't want to write infrastructure config files"**
   - You shouldn't have to. Docker Compose is enough.

2. **"Cloud providers are too complicated"**
   - We abstract away the complexity. You write compose files, we handle the rest.

3. **"Heroku is too expensive"**
   - Usage-based pricing means you pay for what you use.

4. **"I can't see what things cost"**
   - Real-time usage dashboard. Know your costs before the invoice.

5. **"My team can't deploy without me"**
   - Team workspaces with roles. Anyone can deploy.

6. **"Something broke and I can't roll back"**
   - One-click rollbacks to any previous deployment.

---

## Use Cases

### SaaS Application Deployment
Deploy your web app + API + database + background workers. All from one compose file.

### Side Project Hosting
Get your side project live without overthinking infrastructure.

### Client Project Delivery
Deploy client projects with predictable costs and easy handoff.

### Development/Staging Environments
Spin up preview environments for PRs. Tear them down when merged.

### Multi-Service Applications
Microservices, monorepos, whatever. If it runs in Docker, it runs on LazyCloud.

---

## Customer Journey

1. **Awareness** - Frustrated with current deployment options, searching for alternatives
2. **Consideration** - "Wait, I can just use my docker-compose.yaml?"
3. **Trial** - Developer tier (free), deploys first app in minutes
4. **Adoption** - Moves production workloads, invites team
5. **Expansion** - More deployments, custom domains, auto-scaling
6. **Advocacy** - Tells other developers "just use LazyCloud"
