# Provider composition

- Map persisted configuration and process settings to adapters; no workflows.
  Domains own purchase policy, providers own offers, deployments own infrastructure
  and releases, and secret settings own credentials.
- Disabled providers remain constructible for cleanup. Unsupported kinds fail by
  name; never substitute null or stand-in adapters. Resolve bootstrap identities
  when capacity is used, not during bootstrap composition.
- Own release manifest contracts and settings resolution. A complete manifest
  identifies platform/worker images, agent, authorization template and CPU/GPU AMIs.
  Preserve unchanged artifact identities; Docker Bake owns build inventory.
- A deployment without a manifest has no managed capacity. Terraform owns
  infrastructure, Helm runtime settings, and processes consume environment values.
