# Execution Package

Own user execution resources, deterministic planners, Redis collections/signals,
and task/output/volume/secret persistence workflows. Planners stay pure;
services use explicit protocols, wire bodies or explicit arguments, and typed
domain errors. Concrete scheduler, worker, gateway, provider, app, and SDK
implementations remain outside. Accept changes through the real public
SDK/API owner while preserving persistence, authorization, retries, and cleanup.
