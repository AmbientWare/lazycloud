# Agent Package

Own reusable customer/private-pool agent behavior; process arguments and startup
stay in `apps/agent`. Keep API handlers, SDK code, app imports, and broad
composition out. Use narrow protocols for database context and gateway clients,
and accept changes through the real installation, daemon, or telemetry adapter.
