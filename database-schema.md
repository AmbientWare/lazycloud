# Relational database inventory

The reset baseline contains 78 application tables. Alembic owns one additional
revision table. Five unused application tables were removed and two child tables
were added for AWS authorization generations and regional networks.

## Table ownership

| Owner | Tables | Why they remain |
| --- | --- | --- |
| Identity and access | `users`, `user_identities`, `tokens`, `device_authorizations`, `identity_bootstrap_claims`, `identity_admin_recovery_requests`, `workspaces`, `workspace_members`, `workspace_invitations`, `workspace_secrets`, `concurrency_limits`, `workspace_audit_events`, `email_outbox`, `custom_domains` | Account and workspace authority, login, credentials, audit history, reliable email delivery and verified hostnames have different ownership and lifetimes. |
| Applications | `apps`, `stubs`, `deployments`, `app_deployment_intents`, `app_container_shutdown_intents`, `cron_jobs`, `cron_job_runs` | Authored definitions, immutable deployment versions, retryable lifecycle operations and scheduled runs. |
| Execution | `tasks`, `task_attempts`, `task_dependencies`, `containers`, `container_rollout_drains`, `container_billing_shapes`, `endpoint_dispatches`, `pod_urls` | Invocation identity, individual attempts, dependency edges, runtime ownership, safe replacement, recorded billing allocation, request admission and routed ports. |
| Compute | `compute_units`, `compute_capacity_operations`, `compute_provider_instances`, `compute_join_credentials`, `compute_machine_enrollments`, `workspace_compute_policies`, `aws_account_connections`, `aws_authorization_generations`, `aws_account_networks`, `aws_authorization_cleanup_tombstones`, `provider_node_launches`, `machines`, `workers`, `agents`, `agent_leases`, `autoscaler_states`, `autoscaling_targets` | Durable capacity purchases and reconciliation, enrollment authority, provider authorization rotation, fleet identity, agent leases and autoscaler recovery. |
| Storage and images | `objects`, `volumes`, `volume_cleanup`, `images`, `image_archives`, `image_builds`, `image_build_requests`, `image_build_logs`, `checkpoints`, `cache_entries`, `worker_cache_generations`, `source_cache_cleanup_targets`, `storage_access_observations`, `storage_retention_periods` | Object ownership, mounted storage, cleanup after deletion, image access versus shared archives, build execution, checkpoint provenance, physical cache generations and storage metering. |
| Billing | `billing_accounts`, `billing_allowance_periods`, `billing_compute_rates`, `billing_platform_rates`, `billing_credit_adjustments`, `billing_credit_allocations`, `billing_credit_lots`, `billing_credit_settlements`, `billing_ledger_segments`, `billing_meter_outbox`, `billing_plan_change_intents`, `billing_preferences`, `billing_webhook_events`, `credit_purchases` | Published prices, purchased and allocated credits, settled usage, recoverable payment operations and webhook deduplication. Combining these would lose distinct monetary transitions or their history. |
| Observability | `events`, `logs`, `usage_records`, `worker_events` | Product events, output streams, metering observations and worker diagnostics have separate retention and query paths. |

Owner deletion workflows remove live resources and retain the accounting, audit
and cleanup records that still have work or historical meaning. Foreign keys
preserve that distinction through `CASCADE`, `SET NULL`, or intentional historical
identifiers. Recurring queries select active or due work before mapping records.

## Retained JSON

JSON documents use PostgreSQL `JSONB`. No column contains a serialized copy of its
entire owning database record. Mappers reconstruct records from canonical columns.
Runtime string lists, token scopes, commands and similar homogeneous collections
use PostgreSQL arrays.

Every JSON write through the database owner validates JSON encoding, rejects
non-finite numbers and limits the compact UTF-8 document to 1 MiB. Task data uses
a 128 MiB storage ceiling to accommodate dependency result envelopes and base64
encoding. The function boundary still enforces its stricter 16 MiB individual
payload and 64 MiB combined dependency-result limits. Object metadata retains its
existing 2 KiB HTTP limit. These are write limits, not truncation rules; oversized
documents fail with a typed input error and the transaction rolls back.

| Columns | Format and validation owner | Reason for JSON |
| --- | --- | --- |
| `apps.metadata`, `stubs.metadata`, `workspaces.metadata`, `concurrency_limits.metadata` | Domain record maps of JSON values | Caller-defined metadata keys have no fixed relational schema. |
| `workspaces.labels`, `machines.labels`, `workers.labels`, `agents.labels` | Domain record string maps | Caller labels. Machine GPU count has its own integer column. |
| `agents.capacity` | `AgentRecord` map of string or numeric reported values | Diagnostic agent report. Scheduling uses the validated enrollment and worker capacity owners. |
| `autoscaler_states.guardrails`, `last_actions` | Diagnostic map and shared `AutoscaleAction` contracts | One reconciliation's explanation and actions. Counts, decisions, timestamps and target identity are columns. |
| `aws_authorization_generations.authorization_stack`, `aws_authorization_cleanup_tombstones.authorization_stack` | Shared AWS stack creation contract | Provider stack request snapshot. Authority, generation identity, claims and retirement are columns or child relations. |
| `compute_machine_enrollments.preflight_checks` | Shared preflight check contracts | A diagnostic checklist returned as one report. |
| `compute_provider_instances.cost_terms`, `compute_units.offer_cost_terms` | Shared supplier cost quote contract | Immutable pricing snapshot needed to reproduce the purchase. Mutable lifecycle and cost counters are columns. |
| `compute_units.provider_attributes` | Compute unit JSON map, interpreted by its provider | Provider-specific attributes. Shared scheduling and cleanup fields are explicit. |
| `containers.env`, `ports` | `ContainerRecord` string map and integer map | Authored environment and named port maps. |
| `containers.scheduling_payload` | Worker execution message consumed by the runner/worker boundary | Prepared dispatch document for durable replay. Scheduling identity, resources, retries and ownership are columns. Capacity scans do not fetch the document. |
| `custom_domains.required_records` | Shared `DnsRecord` contracts | Provider-supplied DNS verification instructions, consumed together. |
| `deployments.spec` | Shared deployment specification | Immutable authored configuration for that version. |
| `stubs.configuration` | Shared `StubConfig` | Authored image, mount, lifecycle and invocation configuration. Extracted control fields are removed from the document. |
| `stubs.runtime_cpu`, `runtime_memory`, `runtime_disk` | Shared runtime resource contracts | Preserve authored scalar or request/limit pairs and unit-bearing values. Runtime quantities and constraints are validated by the shared contract. |
| `image_builds.image_definition`, `cache_details` | Shared image recipe and build diagnostic map | Authored build instructions and diagnostic output. Build lifecycle, dispatch claims and artifact identities are columns. |
| `objects.metadata`, `write_target_metadata` | Object metadata contracts | Caller metadata and the immutable metadata expected when verifying a write. Repair claims, size, identity and publication are columns. |
| `tasks.args`, `kwargs`, `invocation`, `dependency_bindings`, `function_result`, `result`; `task_attempts.result` | Shared task and function payload contracts | Opaque caller values and encoded invocation/result envelopes. Dependency edges, attempts, retry policy and lifecycle are relational. |
| `events.data`, `worker_events.event_data` | Shared event contracts and JSON maps | Event-specific details. Event kind, time and attribution are columns. |
| `usage_records.labels`, `metadata` | Shared usage record contract | Extra dimensions and diagnostic metadata. Known attribution and metering windows are columns; caller-defined label filters remain JSON expressions. |

External storage credentials use encrypted
columns with a separate cleanup lifetime; tokens use hashes. Existing workspace
secret encryption and provider launch credential encryption share the database's
workspace cipher. SQL client errors hide bound parameter values.

Prepared image dispatch messages are encrypted text through their existing dispatch
owner. They are not an additional mutable image or scheduling record.
