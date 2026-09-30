# Providers

- Use one distribution per provider: `provider-*` packages and `provider_*`
  imports. Translate neutral domain contracts without importing workflow services,
  apps, SDK, database sessions or composition.
- Keep provider branches inside adapters/composition. Purchasable offers and facts
  about owned units are distinct; catalog changes cannot block observation/cleanup.
- Prove support and lifecycle changes through disposable real-provider creation,
  agent enrollment, a workload, deletion and an audit of remaining resources.
