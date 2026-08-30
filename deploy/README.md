# Deployment

Root `compose.yaml` is the canonical local stack. `deploy/platform-eks` owns the
hosted AWS deployment, and `deploy/chart` owns the workloads Argo CD runs there.

## Local Pangolin

Local Compose runs its own Pangolin, Gerbil, Traefik, Newt site connector, and
Pangolin CLI machine client. It uses local volumes and the `.localhost` domain;
it does not read or change the hosted Pangolin installation or its DNS.

Set `LAZYCLOUD_PANGOLIN_SERVER_SECRET` and
`LAZYCLOUD_PANGOLIN_POSTGRES_PASSWORD` before the first boot, then start the
server services:

```sh
docker compose up -d pangolin gerbil pangolin-traefik
docker compose ps pangolin gerbil pangolin-traefik
docker compose logs --tail=80 pangolin gerbil pangolin-traefik
```

Open `https://pangolin.lazycloud.localhost:8443/auth/initial-setup` and use the
one-time setup token from the Pangolin log. Create one organization and an
Integration API key scoped to the organization operations LazyCloud uses. Set
the organization ID and API key in `.env`, then run the idempotent bootstrap:

```sh
docker compose up --build pangolin-bootstrap
```

It creates the Newt site, machine client, `lazycloud.localhost` public resource,
and health-checked `control-plane:9000` target. Their one-time credentials live
only in the `pangolin-runtime` volume; the connector, client, API and scheduler
read the same generated state.

The local stack uses Pangolin Enterprise Edition. Activate its license at
`/admin/license` before testing delegated domains or other Enterprise features.
Pangolin stores the license in its local data volume; it is not a LazyCloud
environment variable.

Start the complete stack after `.env` contains them:

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=80 pangolin-site pangolin-client control-plane agent
```

`pangolin-site` exposes the control-plane Service to Pangolin. The
`pangolin-client` container shares the control plane's network namespace and
reaches private resources exposed by each agent's Newt process. They solve
opposite directions of the connection and both are required.

The API answers on host port 8000 by default. Check the current mapping with
`docker compose port control-plane 9000`.

## Connected AWS acceptance

Use the `default-test` role profile. The root `default` profile is bootstrap
authority and is not an acceptance credential. Compose mounts the profile chain
from `LAZYCLOUD_COMPOSE_AWS_CONFIG_DIR`, which defaults to
`~/.lazycloud/compose-aws`.

The shared fleet and a customer-owned agent exercise different ownership paths.
Do not reuse their fingerprints, bridge subnets, state directories, or pools.

## Resetting local state

PostgreSQL, Redis, Pangolin, and the agent carry related durable identities. A
full predeployment reset must remove all four local volumes or directories in
one scoped operation. Keep image caches only when their durable records still
refer to the same database and Pangolin organization.

Never apply the local reset procedure to an external or hosted deployment.
