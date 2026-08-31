# Deployment

Root `compose.yaml` is the canonical local stack. `deploy/platform-eks` owns the
hosted AWS deployment, and `deploy/chart` owns the workloads Argo CD runs there.

## Local Pangolin

Local Compose runs its own Pangolin, Gerbil, Traefik, Newt site connector, and
Pangolin CLI machine client. It uses local volumes and the `.localhost` domain;
it does not read or change the hosted Pangolin installation or its DNS.

Set `LAZYCLOUD_PANGOLIN_SERVER_SECRET`,
`LAZYCLOUD_PANGOLIN_POSTGRES_PASSWORD`, `LAZYCLOUD_PANGOLIN_API_KEY`,
`LAZYCLOUD_PANGOLIN_ORGANIZATION_ID`, and
`LAZYCLOUD_PANGOLIN_LICENSE_KEY` in the ignored `.env`. The API key uses
Pangolin's `id.secret` form. Start the stack normally:

```sh
docker compose up -d --build
```

The bootstrap creates the local server administrator and organization, activates
the Enterprise license, then reconciles the Newt site, machine client, apex and
wildcard resources, and their health-checked `control-plane:9000` targets. Its
one-time connector credentials live only in `pangolin-runtime`.

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

Pangolin ties an Enterprise license to the host ID in its database. Compose
stores that database under `.lazycloud/pangolin-postgres`, outside its managed
volumes, so `docker compose down -v` preserves the licensed host. Deleting that
directory requires Pangolin to reset the key before it can activate against a
new host ID.

Never apply the local reset procedure to an external or hosted deployment.
