# Cache Server App

Own only settings, cache/storage service composition, and server startup.
Behavior belongs in `packages/cache` and `packages/storage`. Keep environment
names aligned with deployment configuration; accept persistence changes through
a real write/read and restart.
