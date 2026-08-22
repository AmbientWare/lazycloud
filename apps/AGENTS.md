# Deployable apps

`apps/` holds the deployable processes: the HTTP API, the operator CLI, the web
dashboard, and the long-running workers, schedulers, and agents.

An app owns composition and process lifetime: settings, dependency assembly,
request and command adapters, startup and shutdown. It does not own reusable
decisions. Those belong to the domain package the behavior is about, so a second
consumer can reach them without importing an app.

Keep entrypoints thin enough to read in one sitting. When one moves, move its
scripts, probes, manifests, packaging, and callers in the same change and delete
the old path rather than leaving an alias behind.
