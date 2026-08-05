# Cache Server App

Settings, cache and storage service composition, and server startup for the
standalone cache service.

Behavior belongs in `packages/cache` and `packages/storage`; this app wires them
and runs the process. Environment names here are part of the deployment
contract and change together with the deployment assets that set them.
