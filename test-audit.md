# Test audit

One-off audit of every test in the repository against the Test Decision Gate in
`CLAUDE.md`. Each row carries a verdict:

- `keep` — proves current production behaviour at a stable owner or public
  boundary, and passes all four gate criteria.
- `delete` — fails the gate: tests machinery, implementation shape, a removed
  capability, or an invariant already proven at a cheaper authoritative owner.
- `update` — the invariant is worth proving but the test states it wrongly.
  Record why, and how.

Mark a row `- [x]` once its verdict is recorded. Verdicts go in the `verdict`
column; `update` rows carry their reasoning inline.

| owner | count |
|---|---|
| `apps/agent` | 15 |
| `apps/api` | 84 |
| `apps/cli` | 26 |
| `apps/container-worker` | 28 |
| `apps/scheduler` | 3 |
| `apps/worker-bootstrap` | 1 |
| `packages/agent` | 7 |
| `packages/compute` | 67 |
| `packages/control` | 58 |
| `packages/coordination` | 8 |
| `packages/database` | 50 |
| `packages/execution` | 51 |
| `packages/foundation` | 8 |
| `packages/gateway` | 42 |
| `packages/identity` | 49 |
| `packages/images` | 57 |
| `packages/lazycloud` | 192 |
| `packages/networking` | 39 |
| `packages/observability` | 45 |
| `packages/operations` | 26 |
| `packages/provider-clients` | 12 |
| `packages/providers/aws` | 82 |
| `packages/runner` | 16 |
| `packages/scheduler` | 112 |
| `packages/shared` | 126 |
| `packages/storage` | 39 |
| `packages/storage-client` | 7 |
| `packages/worker` | 162 |
| `packages/worker-repository` | 16 |
| `tests` | 250 |
| **pytest total** | **1678** |
| **opt-in E2E modules** | **77** |
| **web test files** | **33** |

---

## pytest

### `apps/agent/tests/test_agent_daemon_preflight.py`

- [x] `test_resource_detection_reports_required_host_preflight` — verdict: keep
- [x] `test_resource_detection_blocks_failed_required_preflight` — verdict: keep

### `apps/agent/tests/test_agent_daemon_readiness.py`

- [x] `test_daemon_writes_runtime_ready_marker_after_first_successful_stream` — verdict: keep
- [x] `test_daemon_removes_stale_ready_marker_before_failed_stream` — verdict: keep
- [x] `test_daemon_cordons_current_session_before_bounded_worker_shutdown` — verdict: keep
- [x] `test_transport_failures_are_recoverable_so_a_machine_keeps_rejoining` — verdict: keep

### `apps/agent/tests/test_agent_daemon_reenrollment.py`

- [x] `test_agent_reenrolls_when_the_control_plane_has_no_identity_for_its_session` — verdict: keep
- [x] `test_a_diverged_enrollment_does_not_end_the_agent_process` — verdict: keep

### `apps/agent/tests/test_agent_metrics.py`

- [x] `test_agent_memory_sample_uses_cgroup_limit_when_lower_than_host` — verdict: keep
- [x] `test_agent_cpu_utilization_normalizes_load_average` — verdict: keep
- [x] `test_agent_metric_sampler_includes_disk_memory_cpu_and_gpu` — verdict: **delete** — Fails gates 1, 2 and 4: every assertion is tautological, vacuous or host-dependent. `snapshot.worker_count == 3` (apps/agent/tests/test_agent_metrics.py:51) echoes its own argument; `free_gpu_count == 2` (:55) echoes `_FakeGpuProvider.available_devices` (:16-17); `memory_total_mb > 0` and `disk_total_mb > 0` (:52-53) read the real host `/proc/meminfo` and `shutil.disk_usage("/")` (apps/agent/src/agent_app/metrics.py:134,157) and only prove the machine has memory and a root filesystem; `disk_path == "/"` (:54) is a host-environment literal. The deterministic invariants it gestures at are already proven by its three siblings in the same file — cgroup memory override at :20, CPU clamp at :40, netdev parsing at :58. `AgentMetricSampler.snapshot` (metrics.py:85-105) exposes no injection seam, so it cannot be made deterministic without adding production seams for the test.
- [x] `test_agent_network_sample_sums_non_loopback_interfaces` — verdict: keep

### `apps/agent/tests/test_agent_worker_configuration.py`

- [x] `test_agent_atomically_writes_worker_yaml_before_starting_container` — verdict: keep
- [x] `test_agent_gives_all_workers_one_bounded_graceful_shutdown_window` — verdict: keep
- [x] `test_agent_stop_treats_concurrent_container_removal_as_settled` — verdict: keep

### `apps/api/tests/test_api_app_scoped_filters.py`

- [x] `test_app_scoped_resource_lists_exclude_peer_apps[stubs]` — verdict: keep
- [x] `test_app_scoped_resource_lists_exclude_peer_apps[deployments]` — verdict: keep
- [x] `test_deployment_pages_are_app_and_workload_scoped_with_opaque_cursors` — verdict: keep
- [x] `test_aggregate_tasks_by_time_window_filters_by_stub_id` — verdict: keep

### `apps/api/tests/test_api_container_metrics.py`

- [x] `test_container_metrics_timeseries_empty_and_missing` — verdict: keep

### `apps/api/tests/test_api_deployment_responses.py`

- [x] `test_deployment_response_exposes_safe_workload_configuration` — verdict: keep

### `apps/api/tests/test_api_error_handling.py`

- [x] `test_missing_resources_return_typed_not_found` — verdict: keep
- [x] `test_invalid_deployment_version_returns_typed_invalid_input` — verdict: keep
- [x] `test_invalid_stub_type_returns_typed_invalid_input` — verdict: keep
- [x] `test_unexpected_exception_returns_opaque_500` — verdict: keep
- [x] `test_unexpected_exception_mints_request_id_when_absent` — verdict: keep

### `apps/api/tests/test_api_event_summaries.py`

- [x] `test_event_target_and_type_normalization` — verdict: **delete** — Fails gate 1 and gate 2. It is an apps/api test that reaches into another owner's internal helpers (`normalize_batch_targets`/`normalize_event_types`, packages/observability/src/observability/event_summary.py:180-198) and asserts whitespace trimming and de-duplication of observability batch-request targets — input shaping, not a stable owner boundary. packages/observability owns its own suite (packages/observability/tests/), so apps/api is not even its owner. Failure would produce duplicate rows in an observability summary panel: no authorization, security, data-integrity, durability, concurrency, cleanup or terminal-outcome impact. The file's remaining content is a dead unused helper (apps/api/tests/test_api_event_summaries.py:35-57), i.e. the residue of a deleted test.

### `apps/api/tests/test_api_gateway_payloads.py`

- [x] `test_container_output_uses_bounded_container_event_lookup` — verdict: **delete** — Mock-transcript and constant assertion, explicitly on the do-not-test list. apps/api/tests/test_api_gateway_payloads.py:76 asserts `events.calls == [("container", container.id, CONTAINER_OUTPUT_EVENT_LIMIT)]` — the recorded call arguments of a hand-written fake (apps/api/tests/test_api_gateway_payloads.py:19-37) compared against the production constant packages/gateway/src/gateway/payloads.py:11. The only behavioural assertion (line 75) is that the right container's stdout comes back, which the loop at packages/gateway/src/gateway/payloads.py:70-79 makes trivially true. No authorization, durability, cleanup or public-contract outcome depends on it.

### `apps/api/tests/test_api_secrets.py`

- [x] `test_secret_routes_mask_lists_and_reveal_only_explicit_detail` — verdict: keep
- [x] `test_secret_mutations_return_exact_conflict_and_not_found_outcomes` — verdict: keep

### `apps/api/tests/test_api_server_and_guards.py`

- [x] `test_control_plane_runtime_start_is_one_shot_under_concurrency` — verdict: keep
- [x] `test_control_plane_runtime_factory_failure_is_terminal` — verdict: keep
- [x] `test_control_plane_health_endpoint` — verdict: keep
- [x] `test_control_plane_health_endpoint_reports_dependency_failure` — verdict: keep
- [x] `test_control_plane_starts_when_tailnet_sidecar_is_not_ready` — verdict: keep

### `apps/api/tests/test_api_shell_tickets.py`

- [x] `test_shell_creation_responses_require_new_single_use_tickets` — verdict: keep
- [x] `test_ticket_store_failure_compensates_without_leaking_ticket_or_store_details[/api/v1/shells/standalone-body0-standalone-container-stub-1]` — verdict: keep
- [x] `test_ticket_store_failure_compensates_without_leaking_ticket_or_store_details[/api/v1/shells/existing-container-body1-existing-container-1]` — verdict: keep
- [x] `test_ticket_store_and_compensation_failure_surface_both_facts_safely` — verdict: keep

### `apps/api/tests/test_api_task_metrics.py`

- [x] `test_task_metrics_api_exposes_percentiles_and_app_filter` — verdict: keep

### `apps/api/tests/test_api_task_resources.py`

- [x] `test_raw_task_creation_is_unsupported_and_historic_commands_cannot_rerun` — verdict: keep
- [x] `test_task_detail_projects_durable_function_result_to_public_result` — verdict: keep

### `apps/api/tests/test_api_web_static.py`

- [x] `test_web_static_serves_spa_without_masking_api_routes` — verdict: keep

### `apps/api/tests/test_aws_connections.py`

- [x] `test_connection_status_is_available_when_aws_mutations_are_disabled` — verdict: keep
- [x] `test_unfinished_setup_can_be_canceled_without_active_authorization` — verdict: keep
- [x] `test_managed_connection_projects_its_nonsecret_stack_identity` — verdict: keep
- [x] `test_terminal_cleanup_failure_is_recoverable_from_the_row` — verdict: keep
- [x] `test_label_only_existing_role_recovery_survives_the_http_boundary` — verdict: keep
- [x] `test_validation_failure_distinguishes_active_health_from_pending_reconnect` — verdict: keep
- [x] `test_degraded_authorization_projects_its_truncated_provider_diagnostic` — verdict: keep
- [x] `test_initial_validation_failure_stays_retryable_without_an_active_generation` — verdict: keep

### `apps/api/tests/test_canonical_compute_capacity_api.py`

- [x] `test_self_hosted_collection_is_static_workspace_scoped_and_excludes_managed_pools` — verdict: keep
- [x] `test_canonical_capacity_routes_enforce_workspace_and_admin_authority` — verdict: keep
- [x] `test_capacity_public_route_adds_nodes_and_patch_extends_the_aggregate` — verdict: keep

### `apps/api/tests/test_capacity_bootstrap.py`

- [x] `test_capacity_bootstrap_reconciles_policy_with_an_immutable_owner` — verdict: keep
- [x] `test_capacity_bootstrap_rejects_duplicate_pool_or_owner_identity` — verdict: keep

### `apps/api/tests/test_compute_policy.py`

- [x] `test_workspace_policy_rejects_unavailable_catalog_selections` — verdict: keep
- [x] `test_compute_inventory_excludes_terminal_history_and_classifies_open_capacity` — verdict: keep
- [x] `test_policy_rejects_aws_default_without_ready_connection` — verdict: keep
- [x] `test_policy_accepts_placement_during_authorization_replacement` — verdict: keep
- [x] `test_explicit_placement_cannot_override_a_self_hosted_pool` — verdict: keep
- [x] `test_managed_placement_binds_workspace_agent_capacity_owner` — verdict: keep
- [x] `test_deployment_placement_is_pinned_when_workspace_default_changes` — verdict: keep

### `apps/api/tests/test_compute_pool_scale.py`

- [x] `test_pool_scale_is_workspace_scoped_and_idempotently_returns_durable_capacity` — verdict: keep

### `apps/api/tests/test_image_build_workspace_isolation.py`

- [x] `test_image_build_http_records_events_and_context_are_workspace_owned` — verdict: keep

### `apps/api/tests/test_operator_workspace_authority.py`

- [x] `test_workspace_token_cannot_forge_operator_workspace_override[POST-/api/v1/containers-payload0]` — verdict: keep
- [x] `test_workspace_token_cannot_forge_operator_workspace_override[POST-/api/v1/pools-payload1]` — verdict: keep
- [x] `test_workspace_token_cannot_forge_operator_workspace_override[POST-/api/v1/machines-payload2]` — verdict: keep
- [x] `test_workspace_token_cannot_forge_operator_workspace_override[GET-/api/v1/cron-jobs-None]` — verdict: keep
- [x] `test_workspace_token_cannot_forge_operator_workspace_override[DELETE-/api/v1/cron-jobs/forged-None]` — verdict: keep
- [x] `test_workspace_token_cannot_forge_operator_workspace_override[GET-/api/v1/secrets/full-None]` — verdict: keep
- [x] `test_workspace_token_cannot_forge_operator_workspace_override[POST-/api/v1/secrets-payload6]` — verdict: keep

### `apps/api/tests/test_provider_node_enrollment.py`

- [x] `test_provider_node_enrollment_rejects_cross_workspace_connection` — verdict: keep
- [x] `test_provider_node_enrollment_rejects_changed_pool_identity` — verdict: keep
- [x] `test_provider_node_bootstrap_failure_is_durable_after_identity_verification` — verdict: keep
- [x] `test_provider_node_bootstrap_phase_is_durable_after_identity_verification` — verdict: keep
- [x] `test_degraded_pool_still_accepts_provider_node_enrollment` — verdict: keep

### `apps/api/tests/test_source_cache_cleanup_status.py`

- [x] `test_source_cache_cleanup_status_is_admin_only_and_bounded` — verdict: keep
- [x] `test_source_cache_cleanup_status_returns_not_found_for_unknown_workspace` — verdict: keep

### `apps/api/tests/test_storage_workspace_isolation.py`

- [x] `test_workspace_object_cleanup_preserves_external_bucket_data` — verdict: keep
- [x] `test_secret_relationships_decode_persisted_cloud_bucket_credentials` — verdict: keep

### `apps/api/tests/test_tenant_isolation_by_construction.py`

- [x] `test_cross_workspace_resource_ids_are_not_found_from_another_workspace` — verdict: keep

### `apps/api/tests/test_token_authority.py`

- [x] `test_workspace_writer_can_issue_an_ordinary_workspace_token` — verdict: keep
- [x] `test_workspace_writer_cannot_issue_a_privileged_token_kind[admin]` — verdict: keep
- [x] `test_workspace_writer_cannot_issue_a_privileged_token_kind[workspace-primary]` — verdict: keep
- [x] `test_workspace_writer_cannot_issue_a_privileged_token_kind[workspace-restricted]` — verdict: keep
- [x] `test_workspace_writer_cannot_issue_a_privileged_token_kind[worker]` — verdict: keep
- [x] `test_workspace_writer_cannot_issue_a_privileged_token_kind[worker-private]` — verdict: keep
- [x] `test_workspace_writer_cannot_issue_a_privileged_token_kind[machine]` — verdict: keep
- [x] `test_admin_can_explicitly_issue_an_admin_token` — verdict: keep
- [x] `test_token_cannot_mutate_its_own_record[POST-/revoke-cannot revoke the authenticating token]` — verdict: keep
- [x] `test_token_cannot_mutate_its_own_record[POST-/toggle-cannot toggle the authenticating token]` — verdict: keep
- [x] `test_token_cannot_mutate_its_own_record[DELETE--cannot delete the authenticating token]` — verdict: keep

### `apps/api/tests/test_workspace_deletion_ephemeral_state.py`

- [x] `test_workspace_deletion_removes_only_its_ephemeral_workload_state` — verdict: keep
- [x] `test_workspace_deletion_ignores_terminal_container_history_with_stale_workers` — verdict: keep

### `apps/api/tests/test_workspace_directory_projection.py`

- [x] `test_admin_current_workspace_honors_explicit_workspace_override` — verdict: keep
- [x] `test_admin_can_include_deleting_workspaces_but_not_deleted_tombstones` — verdict: keep
- [x] `test_workspace_token_cannot_expand_its_workspace_directory` — verdict: keep

### `apps/api/tests/test_workspace_lookup_read_only.py`

- [x] `test_authenticated_missing_workspace_requests_return_404_without_creating_rows` — verdict: keep

### `apps/cli/tests/test_cache_cli_contract.py`

- [x] `test_cache_get_downloads_remote_binary_content` — verdict: keep

### `apps/cli/tests/test_cli.py`

- [x] `test_cli_runtime_error_output_modes_are_stable_and_traceback_free[False]` — verdict: keep
- [x] `test_cli_runtime_error_output_modes_are_stable_and_traceback_free[True]` — verdict: keep
- [x] `test_cli_path_handler_outside_current_directory_is_rejected` — verdict: keep
- [x] `test_cli_start_debug_reraises_runtime_errors` — verdict: keep
- [x] `test_cli_global_flag_normalization_preserves_command_separator` — verdict: keep
- [x] `test_cli_error_normalization_masks_tokens` — verdict: keep
- [x] `test_cli_connection_failures_identify_their_configuration_owner[database-expected_hints0-forbidden_hints0]` — verdict: **delete** — Presentation-literal matrix. The rows assert substrings of the human hint prose returned by `_connection_hint` (apps/cli/src/cli/components/errors.py:48-86) — env-var names and the words "control plane"/"profile endpoint" — at apps/cli/tests/test_cli.py:298-299. tests/CLAUDE.md requires removing presentation-literal tests, and gate 2 fails: a mis-routed diagnostic hint changes advisory copy only; the command still fails with the same `control_plane_unavailable` type and the same exit code regardless of which hint is chosen.
- [x] `test_cli_connection_failures_identify_their_configuration_owner[redis-expected_hints1-forbidden_hints1]` — verdict: **delete** — Same finding as the `database` row: asserts hint prose substrings produced by apps/cli/src/cli/components/errors.py:55-59 at apps/cli/tests/test_cli.py:298-299. Presentation literal with no material failure impact (gate 2).
- [x] `test_cli_connection_failures_identify_their_configuration_owner[object-store-expected_hints2-forbidden_hints2]` — verdict: **delete** — Same finding as the `database` row: asserts hint prose substrings produced by apps/cli/src/cli/components/errors.py:60-67 at apps/cli/tests/test_cli.py:298-299. Presentation literal with no material failure impact (gate 2).
- [x] `test_cli_connection_failures_identify_their_configuration_owner[control-plane-expected_hints3-forbidden_hints3]` — verdict: **delete** — Same finding as the `database` row: asserts hint prose substrings produced by apps/cli/src/cli/components/errors.py:68-72 at apps/cli/tests/test_cli.py:298-299. Presentation literal with no material failure impact (gate 2).

### `apps/cli/tests/test_cli_task_results.py`

- [x] `test_task_result_human_presents_structured_json_value[cli0-lazycloud.cli.resources.task_client]` — verdict: keep
- [x] `test_task_result_human_presents_structured_json_value[cli1-lazycloud.cli.resources.task_client]` — verdict: **delete** — Redundant matrix row that proves composition wiring, not distinct behaviour. `build_admin_cli()` is literally `build_public_cli((_register_operator_cli,))` (apps/cli/src/cli/main.py:88-89) and `_register_operator_cli` never touches the `task` group (apps/cli/src/cli/main.py:110-137), so the `cli1` (public CLI) row executes the exact same command implementation and the same patch target as the `cli0` row already kept at apps/cli/tests/test_cli_task_results.py:78-95. The only thing the second row adds is that the admin CLI inherits the public group — implementation shape.
- [x] `test_task_result_json_preserves_canonical_json_result[cli0-lazycloud.cli.resources.task_client]` — verdict: keep
- [x] `test_task_result_json_preserves_canonical_json_result[cli1-lazycloud.cli.resources.task_client]` — verdict: **delete** — Same finding: duplicate of the `cli0` row at apps/cli/tests/test_cli_task_results.py:105-121, since apps/cli/src/cli/main.py:88-89 and :110-137 show the admin CLI reuses the identical public `task result` implementation.
- [x] `test_task_result_inspection_never_deserializes_cloudpickle[cli0-lazycloud.cli.resources.task_client]` — verdict: keep
- [x] `test_task_result_inspection_never_deserializes_cloudpickle[cli1-lazycloud.cli.resources.task_client]` — verdict: **delete** — Same finding: duplicate of the `cli0` row at apps/cli/tests/test_cli_task_results.py:131-161, which keeps the material security invariant (CLI never calls `pickle.loads` on a task result). The `cli1` row re-runs the same code path via apps/cli/src/cli/main.py:88-89.

### `apps/cli/tests/test_http_command_boundary.py`

- [x] `test_operator_pool_create_sends_explicit_capacity_policy` — verdict: **delete** — Generated-request/mock-transcript test of the CLI option surface. apps/cli/tests/test_http_command_boundary.py:115-126 revalidates the payload captured by `_RecordingHttpChannel` (apps/cli/tests/test_http_command_boundary.py:23-52) and asserts eleven flag-to-field mappings; nothing about resulting state, response, error or residue is observed. tests/CLAUDE.md forbids asserting "plans, commands, mocks" and the root gate forbids "generated commands or plans". The invariant it gestures at — that pool capacity policy is durable and its owner immutable — is proven at its real owner in apps/api/tests/test_capacity_bootstrap.py:47-65 and apps/api/tests/test_compute_pool_scale.py:251-255.
- [x] `test_operator_token_revoke_targets_explicit_workspace` — verdict: keep

### `apps/cli/tests/test_offline_auth.py`

- [x] `test_offline_bootstrap_publishes_once_without_printing_the_secret` — verdict: **update** — Currently failing on a stale expectation, not a defect: `auth bootstrap` now provisions workspace object storage inside the command (apps/cli/src/cli/offline_auth.py:66 builds `S3ObjectStoreClient.from_settings(S3ObjectStoreSettings())` and :99 calls `_provision_workspace_storage`, deliberately moved there per the docstring at :116-131 so API startup does no storage I/O). Unit runs point the object store at a deliberately unroutable endpoint (tests/service_fixtures.py:35, conftest.py:50-53), so the command raises `WorkspaceStorageError` and apps/cli/tests/test_offline_auth.py:50 fails on `exit_code == 0`. The invariant is still worth proving and is CLI-owned: the `--json` payload must never contain the secret (lines 52-53), the replay must report `already_published` with the same token id (56-58), the output file must be mode 0600 (59) and no `.pending` residue may survive (60).
  - **How:** Make the test account for storage provisioning now being part of bootstrap. Preferred: gate it on a real local object store the way `real_redis_actors` gates real Redis (conftest.py:60-73) — an opt-in `LAZYCLOUD_TEST_OBJECT_STORE_*` fixture pointing at the compose MinIO — and keep every assertion. Otherwise assert the new terminal contract instead: that bootstrap fails loudly and names the unreachable object store while still never printing `rt_`, and move the exact-once/0600 credential proof to the identity owner, which already covers it (packages/identity/tests/test_offline_admin_bootstrap.py:72-77). Do not stub `S3ObjectStoreClient`.
- [x] `test_offline_recovery_refuses_non_postgresql_authority` — verdict: keep
- [x] `test_offline_bootstrap_accepts_configured_token_only_through_private_file` — verdict: **update** — Same stale expectation as the sibling bootstrap test: apps/cli/tests/test_offline_auth.py:134 asserts `exit_code == 0`, but `auth bootstrap --token-file` now also runs `_provision_workspace_storage` (apps/cli/src/cli/offline_auth.py:99) against the unroutable unit endpoint (tests/service_fixtures.py:35), so the command exits 1 with `WorkspaceStorageError`. The CLI-owned part of the invariant is still material — the configured secret never appears in `--json` output (line 135) and no credential file is written when `--token-file` is used (line 136); the hash-only storage and canonical-token rules are already proven at packages/identity/tests/test_offline_admin_bootstrap.py.
  - **How:** Same fix as the sibling test: run it against a real local object store through an opt-in fixture modelled on `real_redis_actors` (conftest.py:60-73), or narrow it to the assertions that survive the new failure path (no secret in output, no output file) while letting packages/identity own the configured-credential invariants. Do not introduce a fake object-store client to keep it green.
- [x] `test_configured_token_file_requires_private_mode` — verdict: keep
- [x] `test_configured_token_file_requires_canonical_token` — verdict: keep

### `apps/cli/tests/test_source_cache_cleanup_cli.py`

- [x] `test_workspace_cleanup_status_uses_typed_admin_api_and_clean_json` — verdict: keep
- [x] `test_workspace_cleanup_status_is_operator_only` — verdict: **delete** — Command-surface inventory asserted through help-text literal: apps/cli/tests/test_source_cache_cleanup_cli.py:59-66 invokes `build_public_cli()` and asserts `"No such command" in result.output`. That is an export/route inventory plus literal copy, not an authorization boundary — the real admin-only enforcement is proven at apps/api/tests/test_source_cache_cleanup_status.py:54-57 (workspace token gets 403, admin gets 200). Absence of the command from the public CLI is surface curation, and the operator CLI is a superset by construction (apps/cli/src/cli/main.py:88-89).

### `apps/container-worker/tests/test_container_service_http.py`

- [x] `test_container_service_http_validates_auth_and_decodes_binary_requests` — verdict: keep

### `apps/container-worker/tests/test_worker_configuration.py`

- [x] `test_worker_settings_precedence_is_init_then_env_then_yaml` — verdict: keep
- [x] `test_worker_settings_reject_unknown_yaml_fields` — verdict: keep
- [x] `test_worker_configuration_rejects_non_dedicated_build_root[/dev/shm]` — verdict: keep
- [x] `test_worker_configuration_rejects_non_dedicated_build_root[/dev/shm/builds]` — verdict: keep
- [x] `test_worker_configuration_rejects_non_dedicated_build_root[/cache]` — verdict: keep
- [x] `test_worker_configuration_rejects_non_dedicated_build_root[/cache/builds]` — verdict: keep
- [x] `test_worker_execution_configuration_requires_unique_candidates_and_default` — verdict: keep

### `apps/container-worker/tests/test_worker_main.py`

- [x] `test_shutdown_handler_records_request_before_interrupting_worker` — verdict: **delete** — Fails gates 1 and 3. It exercises the private context manager _container_worker_shutdown_handlers (main.py:527) in isolation and sends a real SIGTERM to the pytest process. The same ordering is proven through the production entrypoint at test_worker_main.py:55-70: _SignalMaskingProcessor raises WorkerRepositoryClientError from inside the handler, and run_container_worker's loop only breaks because shutdown_event was set first (main.py:348-350) - so processor.calls == 1 fails if the event is not recorded before the interrupt, and shutdown_reasons == [StopContainerReason.Preempted] fails if on_signal did not record SIGTERM before the raise (main.py:539-543, :366-376).
- [x] `test_worker_keepalive_defaults_and_bounds_are_lease_safe` — verdict: **update** — The bound itself is worth keeping (a keepalive interval above a third of the lease TTL lets a healthy worker be reaped), but the test opens with a tautology and then restates a derived constant as a literal. test_worker_main.py:45-47 asserts _parse_arguments([]).keepalive_interval_seconds == DEFAULT_WORKER_KEEPALIVE_INTERVAL_SECONDS, and main.py:188-192 sets that argparse default from that same constant - the assertion can only fail if argparse itself is broken. test_worker_main.py:48-51 then hardcodes 20 and matches the literal message 'no more than 20 seconds', while production derives the limit as MAX_WORKER_KEEPALIVE_INTERVAL_SECONDS = DEFAULT_WORKER_KEEPALIVE_TTL_SECONDS / 3 (main.py:50, worker_lifecycle.py:25), so raising the TTL silently invalidates the test's numbers.
  - **How:** Delete the _parse_arguments default assertion entirely - it tests argparse wiring, not a decision. Express the remaining bound against the production constant instead of the literal: assert _validate_keepalive_interval(MAX_WORKER_KEEPALIVE_INTERVAL_SECONDS) is accepted, that a value just above it raises, that 0 raises, and that MAX_WORKER_KEEPALIVE_INTERVAL_SECONDS * 3 <= DEFAULT_WORKER_KEEPALIVE_TTL_SECONDS so the lease-safety relationship survives a TTL change. Match on the error type rather than the interpolated seconds figure.
- [x] `test_worker_stops_when_repository_error_masks_signal_interrupt` — verdict: keep
- [x] `test_worker_renews_lease_while_pickup_is_blocked` — verdict: keep
- [x] `test_worker_does_not_process_when_registration_fails` — verdict: keep
- [x] `test_persistent_worker_registration_rollback_preserves_owner_identity` — verdict: keep

### `apps/container-worker/tests/test_worker_production.py`

- [x] `test_tar_image_archive_archiver_and_mounter_materialize_rootfs` — verdict: keep
- [x] `test_tar_image_archive_mounter_preserves_image_root_symlinks` — verdict: keep
- [x] `test_tar_image_archive_mounter_reuses_existing_mount_without_archive` — verdict: keep
- [x] `test_tar_image_archive_mounter_reuses_concurrent_completed_mount` — verdict: keep
- [x] `test_tar_image_archive_mounter_marks_incomplete_mount_for_repair` — verdict: keep
- [x] `test_tar_image_archive_mounter_rejects_manifest_for_changed_archive` — verdict: keep
- [x] `test_tar_image_archive_mounter_atomically_repairs_incomplete_mount` — verdict: keep
- [x] `test_tar_image_archive_mounter_preserves_incomplete_mount_when_repair_fails` — verdict: keep
- [x] `test_tar_image_archive_mounter_rejects_path_traversal_members` — verdict: keep
- [x] `test_production_worker_requires_repository_credentials_before_registration[-worker-token-worker repository endpoint is required]` — verdict: keep
- [x] `test_production_worker_requires_repository_credentials_before_registration[http://control-plane:9000--worker repository token is required]` — verdict: keep
- [x] `test_brokered_image_source_loader_downloads_presigned_archive` — verdict: keep
- [x] `test_remote_checkpoint_persister_streams_archive_to_presigned_url` — verdict: keep
- [x] `test_checkpoint_transfer_errors_never_disclose_capability_query` — verdict: keep

### `apps/scheduler/tests/test_compute_placement.py`

- [x] `test_scheduler_forwards_typed_ad_hoc_placement_to_capacity_owner` — verdict: **delete** — Fails gate 1 and 3. The first half is a mock transcript: `_RecordingCapacity` (apps/scheduler/tests/test_compute_placement.py:102-117) records the request and returns a hard-coded `ComputePlacement`, so `placed.pool_selector`, `placed.capacity_owner_id` and `placed.placement_source is WorkloadOverride` (:59-62) only assert what the fake just returned, and `capacity.requests[0].deployment_id == ""` (:55) asserts the default of the input model it passed in. `SchedulerComputePlacement` (packages/scheduler/src/scheduler/compute_placement.py:25-55) is a field-mapping adapter; the real placement decision is owned by packages/compute/src/compute/request_placement.py:59 and proven at apps/api/tests/test_compute_policy.py:409. The second half (:64-99) re-proves `ComputePoolCapacityController.accepts` / `reservation_shape` / `plan_acquisition` (packages/scheduler/src/scheduler/capacity_reservations.py:387,409,577) which are already proven at their owner by packages/scheduler/tests/test_scheduler_capacity_reservations.py:692 (`accepts` for cross-workspace/oversized) and :913 (`plan_acquisition` persisting an exact unit). It also sits in apps/scheduler while testing packages/scheduler, against apps/scheduler/CLAUDE.md:4.

### `apps/scheduler/tests/test_scheduler_liveness.py`

- [x] `test_run_scheduler_beats_heartbeat_file_each_iteration` — verdict: keep

### `apps/scheduler/tests/test_scheduler_runtime.py`

- [x] `test_scheduler_runtime_close_resets_owned_token_invalidation` — verdict: **delete** — Fails gates 1, 2 and 4. It constructs `SchedulerRuntime(scheduler=Scheduler(), reset_token_invalidation_on_close=True)` with `owned_services=None` (apps/scheduler/tests/test_scheduler_runtime.py:18-21), so the only code exercised is `configure_token_invalidation(None)` at apps/scheduler/src/scheduler_app/runtime.py:291 — the real cleanup branch (`owned_services.close()` at :288) never runs. The value is a process-global read only by identity/auth.py:326; production sets it once at startup (runtime.py:103, apps/api/src/api/control_runtime.py:174) and clears it at process shutdown, so failing to reset it has no authorization, security, durability or user-visible consequence — it only affects in-process bookkeeping and cross-test isolation. It also consumes the `real_redis_actors` fixture purely to build an `AuthTokenInvalidation` that is never used.

### `apps/worker-bootstrap/tests/test_worker_token_bootstrap.py`

- [x] `test_worker_token_waits_for_admin_and_retries_without_leaking_credentials` — verdict: keep

### `packages/agent/tests/test_agent_planning.py`

- [x] `test_agent_capacity_parsing_gpu_selection_and_schedulable_checks` — verdict: keep
- [x] `test_nvidia_smi_gpu_device_parsing_normalizes_names_and_skips_invalid_rows` — verdict: keep
- [x] `test_agent_service_serializes_worker_capacity_without_credentials` — verdict: keep
- [x] `test_worker_slot_equality_and_reconciliation` — verdict: keep

### `packages/agent/tests/test_agent_service_unit.py`

- [x] `test_agent_service_retries_for_as_long_as_the_machine_exists` — verdict: **delete** — Asserts literal substrings of generated systemd deployment wiring — `"Restart=always" in unit`, `"StartLimitIntervalSec=0" in unit`, `"StartLimitBurst" not in unit` (packages/agent/tests/test_agent_service_unit.py:17-19) — against the fixed line list in packages/agent/src/agent/service_manager.py:398-428. The test's own docstring (packages/agent/tests/test_agent_service_unit.py:6-8) frames it as a regression guard for a since-fixed start-rate-limit bug. That is squarely 'generated commands or plans' plus 'one-time deployment wiring or a fixed bug' on the do-not-test list, and packages/agent/CLAUDE.md names the real installation as the acceptance path for this owner (the unit is rendered for install at apps/cli/src/cli/agent_install.py:318). Gate 1 fails: the assertion is on rendered config shape, not an owner-boundary behaviour.

### `packages/agent/tests/test_binary_settings.py`

- [x] `test_agent_binary_settings_require_atomic_immutable_configuration` — verdict: keep

### `packages/agent/tests/test_tailnet_coordination.py`

- [x] `test_agent_route_proxy_prefers_tailnet_hostname_when_present` — verdict: **delete** — The function under test is dead production code: a repo-wide grep for `plan_agent_route_proxy` returns only its definition at packages/agent/src/agent/operations.py:816 and this test at packages/agent/tests/test_tailnet_coordination.py:14. Real agent route proxying is owned by `AgentRouteProxyService` in apps/agent/src/agent_app/route_proxy.py, wired at apps/agent/src/agent_app/daemon.py:1299 and used at daemon.py:1129 — `agent.operations.AgentRouteProxy`/`AgentRouteProxyPlan` are never constructed by production. The test therefore proves no production behaviour (gate 1) and its failure could not affect any user-visible outcome (gate 2); it also asserts only a generated dial plan's literal URL and metadata (packages/agent/tests/test_tailnet_coordination.py:16-18), which the do-not-test list covers as 'generated commands or plans'.

### `packages/compute/tests/test_aws_connection_lifecycle.py`

- [x] `test_bucket_access_reconciliation_retries_through_durable_connection_claim` — verdict: keep
- [x] `test_uncompleted_setup_removal_hides_connection_and_reconciles_tombstone` — verdict: keep
- [x] `test_cancel_reconnect_preserves_ready_generation_and_placement` — verdict: keep
- [x] `test_initial_assume_role_miss_remains_authorization_required` — verdict: keep
- [x] `test_active_removal_reuses_provider_operation_across_restart_safe_observation` — verdict: keep
- [x] `test_stuck_provider_cleanup_becomes_action_required_after_bounded_attempts` — verdict: keep
- [x] `test_connection_claim_is_exclusive_and_stale_writer_is_fenced` — verdict: keep

### `packages/compute/tests/test_billing.py`

- [x] `test_http_managed_billing_uses_bounded_typed_production_io` — verdict: keep
- [x] `test_http_managed_billing_surfaces_upstream_status_without_soft_success` — verdict: keep

### `packages/compute/tests/test_bucket_access.py`

- [x] `test_aws_deployment_lifecycle_reconciles_aggregate_ambient_bucket_access` — verdict: keep

### `packages/compute/tests/test_capacity_worker_bootstrap.py`

- [x] `test_machine_bootstrap_rejects_credentialed_or_non_https_gateway_urls[http://control.example.com]` — verdict: keep
- [x] `test_machine_bootstrap_rejects_credentialed_or_non_https_gateway_urls[https://user:secret@control.example.com]` — verdict: keep
- [x] `test_machine_bootstrap_rejects_credentialed_or_non_https_gateway_urls[https://control.example.com/path]` — verdict: keep
- [x] `test_machine_bootstrap_rejects_credentialed_or_non_https_gateway_urls[https://control.example.com?token=abc]` — verdict: keep
- [x] `test_registered_worker_boundaries_reject_invalid_capacity_owner_id[]` — verdict: keep
- [x] `test_registered_worker_boundaries_reject_invalid_capacity_owner_id[not-a-uuid]` — verdict: keep
- [x] `test_registered_worker_boundaries_reject_invalid_capacity_owner_id[AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA]` — verdict: keep
- [x] `test_agent_worker_token_reuse_requires_reusable_worker_binding` — verdict: keep

### `packages/compute/tests/test_compute_telemetry.py`

- [x] `test_scoped_telemetry_credentials_are_append_only_and_workspace_scoped` — verdict: keep
- [x] `test_telemetry_redaction_covers_auth_phrases_json_and_credentials` — verdict: keep
- [x] `test_agent_telemetry_token_decisions_cover_invalid_changed_and_accepted` — verdict: keep
- [x] `test_agent_liveness_and_disconnect_decisions_match_heartbeat_rules` — verdict: keep

### `packages/compute/tests/test_pooled_capacity.py`

- [x] `test_internal_pool_scale_enforces_workspace_capacity_limit[0-20-15-15-16-expected_capacity_calls0-False]` — verdict: keep
- [x] `test_internal_pool_scale_enforces_workspace_capacity_limit[5-3-3-3-3-expected_capacity_calls1-False]` — verdict: keep
- [x] `test_internal_pool_scale_enforces_workspace_capacity_limit[0-0-1-0-0-expected_capacity_calls2-True]` — verdict: keep
- [x] `test_aws_default_capacity_is_one_durable_floor_preserved_by_placement` — verdict: keep
- [x] `test_scale_zero_persists_intent_and_retires_sizing_before_provider_mutation` — verdict: keep
- [x] `test_scale_zero_retains_degraded_intent_and_repairs_provider_failure` — verdict: keep
- [x] `test_internal_pool_scale_maps_mutation_coordinator_failure` — verdict: keep
- [x] `test_scale_zero_skips_provider_only_after_durable_convergence` — verdict: keep
- [x] `test_scale_zero_repairs_fresh_provider_drift_without_restoring_nonzero_intent` — verdict: keep
- [x] `test_scale_zero_terminalizes_missing_provider_instance_projections` — verdict: keep
- [x] `test_reconcile_rereads_zero_intent_after_capacity_owner_lease` — verdict: keep
- [x] `test_pooled_reconcile_fails_closed_without_capacity_owner_lease` — verdict: keep
- [x] `test_pooled_capacity_plan_uses_authoritative_desired_state_without_mutation` — verdict: keep
- [x] `test_pooled_capacity_acquisition_is_idempotent_and_releases_only_its_unit` — verdict: keep
- [x] `test_connection_drain_deletes_hidden_capacity_idempotently` — verdict: keep
- [x] `test_pooled_scale_down_waits_for_exact_volume_absence` — verdict: keep
- [x] `test_pooled_scale_down_projects_updating_during_provider_termination` — verdict: keep
- [x] `test_connection_drain_terminalizes_provider_nodes_and_preserves_history` — verdict: keep
- [x] `test_internal_pool_bootstrap_phase_deadline_reclaims_only_after_it_elapses` — verdict: keep
- [x] `test_relaunch_exhaustion_durably_degrades_pool_until_explicit_capacity_mutation` — verdict: keep
- [x] `test_zero_capacity_policy_update_drives_internal_pool_desired_to_zero` — verdict: keep

### `packages/compute/tests/test_provider_capacity_reclaim.py`

- [x] `test_direct_capacity_plan_is_read_only_and_retry_preserves_exact_intent` — verdict: keep
- [x] `test_direct_capacity_plan_rejects_limit_and_fixed_shape_without_mutation` — verdict: keep
- [x] `test_direct_capacity_acquisition_retries_without_launching_a_second_machine` — verdict: keep
- [x] `test_direct_capacity_acquisition_recovers_a_lost_provider_response` — verdict: keep
- [x] `test_repeated_launch_uses_one_aggregate_capacity_request_and_deadline` — verdict: keep
- [x] `test_capacity_extension_only_moves_the_aggregate_deadline_forward` — verdict: keep
- [x] `test_repeated_launch_prices_existing_and_new_nodes_over_aggregate_horizon` — verdict: keep
- [x] `test_capacity_extension_uses_whole_hour_boundaries[2h-0.2-200000]` — verdict: keep
- [x] `test_capacity_extension_uses_whole_hour_boundaries[7201-0.3-300000]` — verdict: keep
- [x] `test_capacity_extension_rejects_cap_below_recorded_pool_spend` — verdict: keep
- [x] `test_capacity_reconciliation_repairs_commitment_without_renewing_past_deadline` — verdict: keep
- [x] `test_capacity_reconciliation_fails_closed_when_durable_cap_is_undercommitted` — verdict: keep
- [x] `test_expired_capacity_retries_provider_termination_without_renewing` — verdict: keep
- [x] `test_machine_cache_retires_only_after_provider_proves_storage_destroyed` — verdict: keep
- [x] `test_failed_launch_compensates_only_the_owning_provider` — verdict: keep
- [x] `test_failed_launch_compensation_failure_is_surfaced` — verdict: keep
- [x] `test_post_launch_commit_failure_is_durable_and_fresh_reconciliation_finishes_cleanup` — verdict: keep
- [x] `test_crash_after_provider_create_is_discovered_bound_and_reclaimed` — verdict: keep
- [x] `test_lost_provider_launch_response_is_discovered_and_reclaimed` — verdict: keep
- [x] `test_stale_machine_reclaim_honors_the_configured_grace_window` — verdict: keep
- [x] `test_never_registered_machine_is_reclaimed_after_the_deadline` — verdict: keep

### `packages/compute/tests/test_source_cache_storage.py`

- [x] `test_storage_owner_remains_incomplete_until_explicit_destruction_evidence` — verdict: keep
- [x] `test_storage_destruction_evidence_is_fenced_to_exact_owner_generation_and_time` — verdict: keep
- [x] `test_machine_without_registered_cache_has_no_cleanup_to_retire` — verdict: keep

### `packages/control/tests/test_app_execution_admission_postgres.py`

- [x] `test_postgresql_app_execution_admission_serializes_container_creation_and_pause` — verdict: keep

### `packages/control/tests/test_app_lifecycle.py`

- [x] `test_app_pause_resume_preserves_explicitly_stopped_deployments_and_delete_cleans_up` — verdict: keep
- [x] `test_task_queue_deployment_stop_preserves_work_and_delete_cancels_it` — verdict: keep
- [x] `test_app_delete_cancels_task_queue_work_and_removes_messages` — verdict: keep
- [x] `test_app_terminal_publication_replays_one_durable_event_with_deterministic_id[before-stream]` — verdict: keep
- [x] `test_app_terminal_publication_replays_one_durable_event_with_deterministic_id[after-stream]` — verdict: keep
- [x] `test_app_multi_deployment_publication_resumes_after_mid_batch_crash` — verdict: keep
- [x] `test_scheduler_reconciles_unfinished_app_from_durable_state_after_restart` — verdict: keep
- [x] `test_app_execution_redis_cleanup_is_exact_and_preserves_peer_apps` — verdict: keep
- [x] `test_disconnected_worker_shutdown_remains_durable_and_retry_cleans_ack_state` — verdict: keep
- [x] `test_active_container_without_durable_worker_fails_closed` — verdict: **update** — States the invariant wrongly against current production; fails 'DID NOT RAISE UpstreamUnavailableError' when run with real Redis (it is gated on `real_redis_actors`, so it has been silently skipped rather than failing). `ContainerService.stop` cancels the scheduler request (packages/execution/src/execution/containers/service.py:462), and `cancel_container_request` always writes the container-cancellation marker through `_fence_container_request` (packages/scheduler/src/scheduler/state.py:1692, 1783-1799), which also drops the request/claim rows; the fence is authoritative because `DISPATCH_CLAIMED_WORKER_REQUEST_SCRIPT` returns -2 once that key exists (packages/scheduler/src/scheduler/state.py:150-153, 131-138). `ContainerShutdownService._wait` therefore treats a never-dispatched container as confirmed (packages/operations/src/operations/container_shutdown.py:144-146), so `pause` succeeds and the app reaches Paused, not CleanupFailed. The assertions at packages/control/tests/test_app_lifecycle.py:636-643 cannot hold. The underlying invariant — an unowned container is fenced rather than waited on, so pause neither hangs nor strands a container — is real, material, and not proven elsewhere (test_disconnected_worker_shutdown_remains_durable_and_retry_cleans_ack_state covers only the worker-owned path).
  - **How:** Assert the fence instead of a confirmation failure: pause completes, the app is Paused, the container record is Stopped, the scheduler cancellation marker exists while the request/claim entries are gone, `AppContainerShutdownIntentRepository.list(app_id=...)` is empty, and `redis.scan(redis.key('worker-events','pending') + ':*') == []` (no stop delivery was created for a container no worker owns).
- [x] `test_app_and_deployment_http_actions_follow_authorization_and_lifecycle_state` — verdict: keep

### `packages/control/tests/test_deployment_services.py`

- [x] `test_deployment_versions_are_scoped_by_kind_and_keep_versioned_stubs` — verdict: keep
- [x] `test_registration_failure_tombstones_deployment_and_reconciles_placement` — verdict: keep
- [x] `test_registration_and_placement_cleanup_failures_are_both_reported` — verdict: keep
- [x] `test_new_deployment_version_keeps_prior_versions_invokable` — verdict: keep
- [x] `test_invoke_target_never_falls_back_when_latest_version_is_stopped` — verdict: keep
- [x] `test_cron_schedule_follows_deployment_lifecycle` — verdict: keep
- [x] `test_cron_schedule_is_deleted_with_app` — verdict: keep
- [x] `test_deployment_versions_are_scoped_by_app_slug` — verdict: keep
- [x] `test_management_stop_and_delete_are_workspace_scoped_and_stop_containers` — verdict: keep
- [x] `test_deployment_manifest_route_serves_invoke_schema` — verdict: keep

### `packages/control/tests/test_event_bus.py`

- [x] `test_event_bus_send_uses_deterministic_ids_ttl_and_duplicate_guard` — verdict: keep
- [x] `test_event_bus_claims_lock_and_successful_handler_deletes_event` — verdict: keep
- [x] `test_event_bus_missing_callback_republishes_and_releases_lock_without_delete` — verdict: keep
- [x] `test_event_bus_failed_handler_resends_until_retry_limit` — verdict: keep

### `packages/control/tests/test_event_stream_state.py`

- [x] `test_redis_event_stream_repository_reads_and_blocks_on_generic_streams` — verdict: keep

### `packages/control/tests/test_metadata.py`

- [x] `test_sandbox_deployment_preserves_startup_network_policy` — verdict: keep

### `packages/control/tests/test_public_stub_clone.py`

- [x] `test_public_stub_config_allows_public_and_same_workspace_private_only` — verdict: keep
- [x] `test_public_clone_copies_local_object_and_remaps_target_workspace_refs` — verdict: keep
  - **PRODUCTION BUG:** Cloning any public stub that carries a package object is broken: POST /api/v1/stubs/{id}/clone returns 409 'artifact is unavailable: <object_id>'. `ControlPlaneService.clone_stub` creates the cloned stub in the TARGET workspace while its config still references the SOURCE workspace's object id (packages/control/src/control/service.py:744, 758-767), and `create_stub` runs `CleanupRepository.assert_stub_config_available` scoped to the target workspace (packages/control/src/control/service.py:570-573). `assert_references_available` selects ObjectTable rows by `workspace_id == target` (packages/database/src/database/repositories/cleanup.py:56-68) and raises ConflictError for the missing id (packages/database/src/database/repositories/cleanup.py:87-88). Objects are only copied and remapped afterwards, at packages/control/src/control/service.py:768-781, so the availability assertion can never pass on the first write. Fix the ordering — copy and remap the object references before creating the cloned stub, or assert availability against the source workspace for references that are still source-owned. The test is correct as written; do not weaken it.
- [x] `test_cross_workspace_private_clone_is_denied` — verdict: keep
- [x] `test_deployment_package_download_streams_local_file_and_redirects_presigned` — verdict: **update** — Stale after the object bucket taxonomy moved; currently fails 400 != 307. The test inserts object rows with bucket 'packages' directly through the repository (packages/control/tests/test_public_stub_clone.py:199, 208, 284-306), but `ObjectStorage` now allows only WORKSPACE_OBJECT_BUCKET / IMAGE_BUILD_CONTEXT_BUCKET / SOURCE_PACKAGE_BUCKET / the configured default (packages/storage/src/storage/service.py:278-286), and `_validate_bucket` raises InvalidInputError for anything else (packages/storage/src/storage/service.py:1025-1027). `generate_presigned_get_url_for_workspace` hits that check via `get_for_workspace` (packages/storage/src/storage/service.py:733, 481), so the redirect assertion at test_public_stub_clone.py:241 sees a 400. The presign expectations at test_public_stub_clone.py:242-243 are stale as well: production presigns `physical_bucket(bucket)` and `workspaces/<workspace_id>/<bucket>/<key>` (packages/storage/src/storage/service.py:734-737, 1008-1023), never ('packages', 'remote.pkg'). The invariant is still worth proving — a local package streams, a remote one 307-redirects to a workspace-scoped presigned URL.
  - **How:** Create both object rows under `SOURCE_PACKAGE_BUCKET` (shared.app_identity) instead of 'packages', and assert the redirect location and the recorded presign call against `services.object_storage.physical_bucket(bucket)` and `physical_key_for_workspace(workspace_id, bucket=..., key=...)` rather than the raw bucket/key pair.

### `packages/control/tests/test_workspace_change_publishers.py`

- [x] `test_hot_updates_do_not_publish_workspace_change_noise` — verdict: keep
- [x] `test_cron_execution_publishes_after_last_and_next_run_persist` — verdict: keep
- [x] `test_concurrency_counter_publishes_only_committed_changes` — verdict: keep

### `packages/control/tests/test_workspace_change_stream.py`

- [x] `test_workspace_change_stream_requires_authentication` — verdict: keep
- [x] `test_workspace_change_stream_resumes_and_isolates_workspaces` — verdict: keep
- [x] `test_workspace_change_stream_starts_at_current_tail` — verdict: keep
- [x] `test_workspace_change_stream_rejects_invalid_resume_cursor` — verdict: keep
- [x] `test_workspace_change_repository_bounds_and_deletes_workspace_streams` — verdict: keep
- [x] `test_workspace_change_publication_failure_is_nonfatal` — verdict: keep

### `packages/control/tests/test_workspace_deletion.py`

- [x] `test_workspace_deletion_tombstones_identity_and_invalidates_tokens` — verdict: keep
- [x] `test_workspace_deleting_transition_atomically_revokes_credentials_and_device_codes` — verdict: keep
- [x] `test_workspace_deletion_rolls_back_when_audit_append_fails` — verdict: keep
- [x] `test_workspace_deletion_purges_owned_resources_and_protects_identity_scopes` — verdict: keep
- [x] `test_workspace_deletion_purges_autoscaler_state_and_fences_stale_reconciliation` — verdict: keep
- [x] `test_autoscaler_state_write_requires_active_workspace` — verdict: keep
- [x] `test_workspace_deletion_preserves_historical_events_after_resource_cleanup` — verdict: keep
- [x] `test_workspace_deletion_api_requires_admin_and_returns_no_content` — verdict: keep
- [x] `test_workspace_deletion_terminates_managed_provider_capacity` — verdict: **update** — Asserts a capability production deliberately removed; currently fails 409 != 204. Workspace deletion no longer terminates a tenant's running capacity: `WorkspaceDeletionService._assert_compute_disconnected` raises ConflictError naming every non-Deleted pool, with the comment 'Deletion drains drained pools; it never terminates running capacity on the tenant's behalf' (apps/api/src/api/server/workspace_deletion.py:104-114). The test launches a live pool (packages/control/tests/test_workspace_deletion.py:553-562) and then expects 204 at test_workspace_deletion.py:571 and empty provider machines at test_workspace_deletion.py:575, neither of which production can produce. The paid-capacity refusal is still worth proving and is not covered elsewhere — test_workspace_deletion_requires_aws_account_disconnect exercises only the AWS-connection branch (workspace_deletion.py:105-106).
  - **How:** Assert the live-pool refusal instead: DELETE returns 409 naming 'tenant-pool', the workspace remains Active, and `provider.list_machines('tenant-pool')` still holds the machine (no capacity terminated behind the tenant's back). Then release/delete the pool through its owner and assert the retried deletion returns 204 and leaves no provider machines.
- [x] `test_workspace_deletion_requires_aws_account_disconnect` — verdict: keep
- [x] `test_workspace_deletion_aborts_when_object_removal_is_not_confirmed` — verdict: keep
- [x] `test_workspace_deletion_keeps_durable_source_cleanup_when_wake_delivery_fails` — verdict: keep
- [x] `test_concurrent_upload_and_workspace_deletion_converges_without_orphan` — verdict: keep

### `packages/control/tests/test_workspace_directory_projection.py`

- [x] `test_workspace_directory_projects_active_deleting_and_deleted_lifecycles` — verdict: keep

### `packages/control/tests/test_workspace_lookup.py`

- [x] `test_control_workspace_reads_are_empty_or_not_found_without_creating_rows` — verdict: keep

### `packages/control/tests/test_workspace_settings.py`

- [x] `test_workspace_rename_and_token_actions_write_attributed_audit_history` — verdict: keep
- [x] `test_workspace_rename_requires_write_scope_and_valid_name` — verdict: keep

### `packages/coordination/tests/test_invalidation.py`

- [x] `test_invalidation_generation_starts_at_zero_and_bumps_monotonically` — verdict: **delete** — Already proven at the consumer owner, and its unique residue is a key constant. packages/coordination/tests/test_invalidation.py:17 asserts the literal key `"test:invalidation:auth-tokens"`; lines 18-21 assert start-at-zero and monotonic bump against a counter the test file implements itself (packages/coordination/tests/test_invalidation.py:51-56). The same primitive is exercised end to end through `AuthTokenInvalidation.from_redis` at packages/identity/tests/test_token_invalidation.py:38-51 (revoked token rejected across replicas) and :69-111 (every validity mutation strictly increases the generation, creation does not), which is the authoritative consumer proof packages/coordination/AGENTS.md defers to.
- [x] `test_invalidation_generation_reads_bytes_and_propagates_redis_errors` — verdict: keep

### `packages/coordination/tests/test_redis_coordination.py`

- [x] `test_redis_client_name_sanitization_removes_protocol_unsafe_characters` — verdict: keep
- [x] `test_redis_settings_build_explicit_tcp_tls_and_unix_clients` — verdict: keep
- [x] `test_redis_settings_reject_implicit_query_knobs_and_invalid_urls` — verdict: keep
- [x] `test_redis_wake_signal_coalesces_concurrent_signals_and_blocks_for_consumption` — verdict: **delete** — Proves a fake, and is already proven against real Redis. The `_WakeRedis.eval` fake at packages/coordination/tests/test_redis_coordination.py:186-198 discards the production Lua script (`del script`) and reimplements coalescing in Python under a threading.Lock, so it cannot prove the script's atomicity (gate 1). The identical invariant — 16 concurrent `signal()` calls coalesce to exactly one queued wake, `wait()` consumes it once — is proven against real Redis at packages/coordination/tests/test_redis_coordination.py:118-131, which packages/coordination/AGENTS.md names as the required boundary for atomicity behaviour. It also asserts a mock transcript, `fake.blocking_timeouts == [0.25, 0.25]` (line 78).
- [x] `test_token_lock_rejects_empty_tokens_and_non_positive_ttl` — verdict: keep
- [x] `test_real_redis_scripts_locks_and_pubsub_leave_no_keys` — verdict: keep

### `packages/database/tests/test_cache_entry_repository.py`

- [x] `test_cache_entry_upsert_preserves_identity_and_normalizes_sqlite_datetimes` — verdict: keep
- [x] `test_cache_entry_repository_rejects_inverted_relational_timestamps` — verdict: keep
- [x] `test_cache_entry_repository_increments_hits_in_the_database` — verdict: keep

### `packages/database/tests/test_cache_entry_repository_postgres.py`

- [x] `test_postgresql_concurrent_cache_upsert_preserves_one_canonical_row` — verdict: keep
- [x] `test_postgresql_cache_hit_increment_is_atomic` — verdict: keep

### `packages/database/tests/test_checkpoint_repository.py`

- [x] `test_checkpoint_repository_lifecycle_uses_database` — verdict: keep
- [x] `test_checkpoint_repository_requires_durable_expiration_before_pruning` — verdict: keep
- [x] `test_checkpoint_retention_selects_only_published_or_terminal_records` — verdict: keep

### `packages/database/tests/test_compute_provider_instance_repository.py`

- [x] `test_provider_instance_machine_binding_is_idempotent_and_fenced` — verdict: keep

### `packages/database/tests/test_identity_device_authorization_repository.py`

- [x] `test_device_authorization_unique_collision_preserves_transaction` — verdict: keep
- [x] `test_device_authorization_constraints_reject_invalid_state[pending-workspace-False]` — verdict: keep
- [x] `test_device_authorization_constraints_reject_invalid_state[pending-None-True]` — verdict: keep
- [x] `test_device_authorization_constraints_reject_invalid_state[approved-None-False]` — verdict: keep
- [x] `test_device_authorization_constraints_reject_invalid_state[denied-workspace-False]` — verdict: keep
- [x] `test_device_authorization_constraints_reject_invalid_state[expired-None-False]` — verdict: keep

### `packages/database/tests/test_identity_secret_repository.py`

- [x] `test_secret_repository_mutations_have_exact_outcomes_and_stable_identity` — verdict: keep
- [x] `test_secret_repository_isolates_same_name_and_cascades_workspace_delete` — verdict: keep
- [x] `test_postgresql_secret_concurrency_and_current_schema` — verdict: keep

### `packages/database/tests/test_identity_token_repository.py`

- [x] `test_consumed_token_requires_terminal_revocation[active-revoked_at0]` — verdict: keep
- [x] `test_consumed_token_requires_terminal_revocation[revoked-None]` — verdict: keep

### `packages/database/tests/test_metrics_repository.py`

- [x] `test_metric_writes_keep_single_latest_row` — verdict: keep

### `packages/database/tests/test_pod_url_repository.py`

- [x] `test_pod_url_upsert_preserves_identity_timestamps_port_and_cascade` — verdict: keep
- [x] `test_postgresql_concurrent_pod_url_upsert_preserves_one_identity` — verdict: keep

### `packages/database/tests/test_pool_capacity_policy_repository.py`

- [x] `test_pool_repository_preserves_owner_on_policy_update_and_rejects_replacement` — verdict: keep

### `packages/database/tests/test_provider_repository_postgres.py`

- [x] `test_postgresql_provider_constraints_reject_noncanonical_authority[non-aws-generic-payload0]` — verdict: keep
- [x] `test_postgresql_provider_constraints_reject_noncanonical_authority[missing-kind-aws-payload1]` — verdict: keep
- [x] `test_postgresql_provider_constraints_reject_noncanonical_authority[divergent-aws-payload2]` — verdict: keep
- [x] `test_postgresql_provider_constraints_reject_noncanonical_authority[wrong-type-aws-payload3]` — verdict: keep
- [x] `test_postgresql_provider_constraints_reject_noncanonical_authority[nested-authority-aws-payload4]` — verdict: keep

### `packages/database/tests/test_readiness.py`

- [x] `test_database_readiness_requires_exact_repository_head` — verdict: keep
- [x] `test_database_readiness_times_out_with_typed_observation` — verdict: keep
- [x] `test_database_readiness_rejects_unbounded_polling_inputs[0.0-1.0-timeout_seconds must be greater than zero]` — verdict: keep
- [x] `test_database_readiness_rejects_unbounded_polling_inputs[1.0-0.0-poll_interval_seconds must be greater than zero]` — verdict: keep

### `packages/database/tests/test_schema_bootstrap.py`

- [x] `test_empty_database_bootstraps_once_through_the_current_baseline` — verdict: keep
- [x] `test_nonempty_noncurrent_database_fails_closed_without_mutation[application-without-revision]` — verdict: keep
- [x] `test_nonempty_noncurrent_database_fails_closed_without_mutation[revision-without-schema]` — verdict: keep
- [x] `test_stale_full_schema_is_never_upgraded_or_reset` — verdict: keep
- [x] `test_postgresql_baseline_matches_metadata_constraints_and_indexes` — verdict: keep

### `packages/database/tests/test_source_cache_cleanup_repository_postgres.py`

- [x] `test_postgresql_source_cache_cleanup_lifecycle_is_fenced_and_restart_safe` — verdict: keep
- [x] `test_postgresql_cleanup_targets_only_global_and_matching_private_generations` — verdict: **update** — Stale expectation after production moved; currently fails against real PostgreSQL. `WorkspaceRepository.purge_owned_records` now refuses any workspace that is not already `Deleting`/`Deleted` (packages/database/src/database/repositories/identity.py:371-379, raising `ConflictError: workspace cleanup requires deleting state`), but the test calls it on a freshly created active workspace at packages/database/tests/test_source_cache_cleanup_repository_postgres.py:376, so it raises before reaching the assertions at :377-378. The correct order is the one the sibling suite already uses — `lock_for_deletion` then `mark_deleting` then `purge_owned_records` (packages/database/tests/test_workspace_deletion_repository.py:172-174 and :257-259). The invariant it protects — cleanup targets fan out only to the global generation plus the owning workspace's private generation, and survive the owner's purge — is a real data-loss/tenant-scope invariant and is worth keeping.
  - **How:** Before line 376, take the workspace through its real deletion transition: `workspace = workspaces.lock_for_deletion(owner.id)` then `workspaces.mark_deleting(workspace)`, then call `workspaces.purge_owned_records(owner.id)`. Leave the assertions at :377-378 (generation retained, both targets retained) unchanged.
- [x] `test_postgresql_cleanup_claims_are_disjoint_and_tombstones_survive_workspace_purge` — verdict: **update** — Same stale expectation, and it currently fails against real PostgreSQL. `purge_owned_records` is called on an active workspace at packages/database/tests/test_source_cache_cleanup_repository_postgres.py:454 and raises `ConflictError: workspace cleanup requires deleting state` from packages/database/src/database/repositories/identity.py:378, so the run aborts before `workspace_repository.tombstone(workspace)` (:455) and before the tombstone-survival and FK/ondelete assertions (:456-482). Everything before the purge — the rolled-back `add_targets` (:404-421) and the disjoint concurrent `claim_due` batches (:428-447) — is a genuine concurrency/data-loss proof and must survive; only the purge sequencing is wrong.
  - **How:** At line 449-455, drive the deletion through its real transition before purging: `workspace_row = workspace_repository.lock_for_deletion(workspace.id)`, `workspace_repository.mark_deleting(workspace_row)`, then `purge_owned_records(workspace.id)` and `tombstone(...)`. Keep the `deletion_blockers` check at :451, the tombstone-survival assertion at :456-457 and the RESTRICT foreign-key / no-`payload`-column assertions at :459-482 unchanged.

### `packages/database/tests/test_state_repositories.py`

- [x] `test_compute_state_repository_tracks_pools_agents_slots_and_ttls` — verdict: keep
- [x] `test_compute_state_repository_deletes_exact_workspace_residue` — verdict: keep

### `packages/database/tests/test_tenant_scoped_repositories.py`

- [x] `test_cross_workspace_reads_and_deletes_are_denied_by_construction` — verdict: keep
- [x] `test_image_archive_identity_is_exact_and_tenant_scoped` — verdict: keep
- [x] `test_container_shutdown_targets_include_only_active_workspace_rows` — verdict: keep

### `packages/database/tests/test_workspace_deletion_repository.py`

- [x] `test_postgresql_workspace_deletion_serializes_complete_attempts` — verdict: keep
- [x] `test_postgresql_workspace_deletion_fences_owned_write_races` — verdict: keep
- [x] `test_postgresql_same_object_location_in_sibling_workspaces_does_not_serialize` — verdict: keep

### `packages/database/tests/test_workspace_resolution.py`

- [x] `test_service_context_workspace_lookup_never_creates_missing_rows` — verdict: keep

### `packages/execution/tests/test_app_execution_admission.py`

- [x] `test_paused_app_rejects_every_execution_producer_without_container_orphans[function]` — verdict: keep
- [x] `test_paused_app_rejects_every_execution_producer_without_container_orphans[endpoint]` — verdict: keep
- [x] `test_paused_app_rejects_every_execution_producer_without_container_orphans[taskqueue]` — verdict: keep

### `packages/execution/tests/test_container_preemption.py`

- [x] `test_function_preemption_uses_explicit_retry_policy` — verdict: keep
- [x] `test_endpoint_preemption_fails_without_blind_replay` — verdict: keep

### `packages/execution/tests/test_container_service.py`

- [x] `test_runtime_assignment_separates_operational_and_compute_ownership` — verdict: keep
- [x] `test_checkpoint_gpu_limit_rejects_before_scheduler_submission` — verdict: keep
- [x] `test_container_stop_targets_only_assigned_worker` — verdict: keep
- [x] `test_container_stop_does_not_broadcast_for_unassigned_request` — verdict: keep
- [x] `test_container_stop_failure_never_persists_success[cancellation-scheduler cancellation failed]` — verdict: keep
- [x] `test_container_stop_failure_never_persists_success[delivery-event delivery failed]` — verdict: keep

### `packages/execution/tests/test_endpoint_service.py`

- [x] `test_endpoint_uses_latest_available_workspace_checkpoint` — verdict: keep

### `packages/execution/tests/test_function_cron_service.py`

- [x] `test_function_cron_rejects_a_stub_from_another_deployment` — verdict: keep

### `packages/execution/tests/test_function_retry_lifecycle.py`

- [x] `test_function_retry_waits_for_previous_container_to_become_terminal` — verdict: keep

### `packages/execution/tests/test_pod_checkpoint_service.py`

- [x] `test_checkpoint_enabled_pod_uses_latest_available_checkpoint` — verdict: keep

### `packages/execution/tests/test_pod_service_readiness.py`

- [x] `test_wait_for_container_client_reloads_durable_terminal_state` — verdict: keep
- [x] `test_mark_container_running_preserves_compute_foreign_keys_and_runtime_assignment` — verdict: keep
- [x] `test_sandbox_exposure_rejects_cross_workspace_stub_before_worker_callback` — verdict: keep
- [x] `test_stub_visibility_mutation_rewrites_only_its_persisted_sandbox_urls` — verdict: keep

### `packages/execution/tests/test_redis_collections.py`

- [x] `test_real_redis_map_round_trip_stats_and_workspace_cleanup` — verdict: keep
- [x] `test_real_redis_simple_queue_round_trip_stats_and_workspace_cleanup` — verdict: keep

### `packages/execution/tests/test_signal_service.py`

- [x] `test_real_redis_signal_round_trip_ttl_and_workspace_cleanup` — verdict: keep
- [x] `test_redis_signal_service_surfaces_repository_errors` — verdict: keep

### `packages/execution/tests/test_task_callbacks.py`

- [x] `test_terminal_tasks_deliver_signed_callback_for_supported_workloads[function]` — verdict: keep
- [x] `test_terminal_tasks_deliver_signed_callback_for_supported_workloads[cron-job]` — verdict: **delete** — Matrix row that re-proves an identical code path. `CALLBACK_SUPPORTED_STUB_KINDS` (packages/execution/src/execution/callbacks.py:31-38) is a flat membership set consulted once at packages/execution/src/execution/callbacks.py:219; the delivery path after that point does not branch on stub kind, and the test body (create stub, transition Running -> Complete) is byte-identical per row. The parametrize list is a transcription of that constant, i.e. an inventory assertion, and the rows protect no distinct transition. The [function] row is retained as the single focused case; the matrix never proves the material negative (an unsupported kind such as Pod/Sandbox must NOT deliver), so nothing is lost.
- [x] `test_terminal_tasks_deliver_signed_callback_for_supported_workloads[endpoint]` — verdict: **delete** — Same duplicate matrix row: the kind is only used for the membership test at packages/execution/src/execution/callbacks.py:219 against the constant set at packages/execution/src/execution/callbacks.py:31-38, and the signing/idempotency/event assertions are already proven once by the [function] row.
- [x] `test_terminal_tasks_deliver_signed_callback_for_supported_workloads[asgi]` — verdict: **delete** — Same duplicate matrix row: no kind-specific branch exists downstream of packages/execution/src/execution/callbacks.py:219, so this row re-executes the identical transition already covered by the [function] row.
- [x] `test_terminal_tasks_deliver_signed_callback_for_supported_workloads[task-queue]` — verdict: **delete** — Same duplicate matrix row. Additionally, task-queue callback delivery on the retry path is already proven independently by packages/execution/tests/test_task_callbacks.py:118-162 (test_retry_callback_uses_bounded_delivery_retries_and_stable_idempotency), which builds a TaskQueue stub.
- [x] `test_retry_callback_uses_bounded_delivery_retries_and_stable_idempotency` — verdict: keep
- [x] `test_permanent_callback_failure_is_observable_without_exposing_target_query` — verdict: keep
- [x] `test_callback_target_rejects_unsafe_url_shapes[file:///tmp/callback]` — verdict: keep
- [x] `test_callback_target_rejects_unsafe_url_shapes[https://user:password@example.com/task]` — verdict: keep
- [x] `test_callback_target_rejects_unsafe_url_shapes[https://example.com/task#fragment]` — verdict: keep
- [x] `test_callback_target_rejects_unsafe_url_shapes[https://example.com:invalid/task]` — verdict: keep
- [x] `test_callback_sender_rejects_hosts_with_private_dns_answers` — verdict: keep

### `packages/execution/tests/test_task_rerun.py`

- [x] `test_historic_command_task_cannot_be_rerun` — verdict: keep

### `packages/execution/tests/test_taskqueue_service.py`

- [x] `test_task_queue_pop_leases_and_complete_acks_after_result_persisted` — verdict: keep
- [x] `test_task_queue_invocation_decoder_rejects_malformed_envelopes` — verdict: keep
- [x] `test_task_queue_state_owns_wait_age_and_live_consumer_capacity` — verdict: keep
- [x] `test_task_queue_expiration_marks_pending_task_and_acks_message` — verdict: keep
- [x] `test_task_queue_expiration_does_not_interrupt_running_task` — verdict: keep
- [x] `test_task_queue_runner_releases_retryable_failures_without_losing_message` — verdict: keep
- [x] `test_task_queue_monitor_tracks_cancelled_status_and_acks_claim` — verdict: keep
- [x] `test_task_queue_monitor_refresh_preserves_active_container_ownership` — verdict: keep
- [x] `test_task_queue_preemption_releases_once_for_existing_retry_policy` — verdict: **update** — Currently FAILS. The test never persists a container row for TASK_QUEUE_CONTAINER_ID, so `tasks.start` drops the attribution (`persisted_container_id = resolved_container_id if _container_exists(...) else None`, packages/execution/src/execution/tasks.py:288-290). `finish_with_retry` then bails out at packages/execution/src/execution/tasks.py:393-397 (`completion does not own the active task container`), so `task_queue_preempted` returns changed=False/stale_attempt=True and the assertion at packages/execution/tests/test_taskqueue_service.py:448 fails. Production is correct: `start_task_queue_serve` creates the consumer container row (packages/execution/src/execution/taskqueues/service.py:565) before any pop. The invariant (a preempted retryable task releases its queue message exactly once, with a fenced delay) is a message-loss/duplicate-delivery invariant worth keeping.
  - **How:** Before `service.task_queue_pop(...)` at packages/execution/tests/test_taskqueue_service.py:403, upsert a `ContainerRecord(id=TASK_QUEUE_CONTAINER_ID, workspace_id=stub.workspace_id, stub_id=stub.id, status=ContainerStatus.Running)` via `ContainerRepository`, exactly as `test_task_queue_state_owns_wait_age_and_live_consumer_capacity` already does at packages/execution/tests/test_taskqueue_service.py:205-216. Verified: with the container row present the service returns changed=True, retry_scheduled=True, message_released=True, locks_cleared=True, status=Retry, attempt_number=1, max_attempts=2 — every existing assertion then holds unchanged.
- [x] `test_task_queue_preemption_acknowledges_non_retryable_attempt_once` — verdict: **update** — Currently FAILS at packages/execution/tests/test_taskqueue_service.py:505 for the same reason as the sibling preemption test: no container row exists for TASK_QUEUE_CONTAINER_ID, so `tasks.start` never records the attribution (packages/execution/src/execution/tasks.py:288-290) and `finish_with_retry` returns the unchanged outcome at packages/execution/src/execution/tasks.py:393-397. The invariant (a non-retryable preemption acks the queue message exactly once and is idempotent on replay) is a duplicate-execution invariant and should be preserved.
  - **How:** Seed the consumer `ContainerRecord` for TASK_QUEUE_CONTAINER_ID before `task_queue_pop` at packages/execution/tests/test_taskqueue_service.py:476 (same pattern as packages/execution/tests/test_taskqueue_service.py:205-216). No assertion changes are needed.
- [x] `test_task_queue_preemption_preserves_authoritative_terminal_state[cancelled]` — verdict: keep
- [x] `test_task_queue_preemption_preserves_authoritative_terminal_state[timeout]` — verdict: keep
- [x] `test_task_queue_preemption_preserves_and_releases_authoritative_retry` — verdict: keep
- [x] `test_task_queue_preemption_rejects_stale_container_attempt` — verdict: keep
- [x] `test_task_queue_preemption_defers_to_newer_database_attempt_owner` — verdict: **update** — Passes, but vacuously. The test sets `task.kwargs["container_id"] = CURRENT_TASK_QUEUE_CONTAINER_ID` (packages/execution/tests/test_taskqueue_service.py:619) as its supposed newer owner, yet the durable fencing check reads `current.container_id`, not kwargs (packages/execution/src/execution/tasks.py:387/393). Nothing in production consults `kwargs["container_id"]` for ownership; it is only a scheduling breadcrumb (packages/execution/src/execution/containers/scheduling.py:139). The test only reports stale_attempt because no container row exists at all, so `tasks.start` never persisted any attribution (packages/execution/src/execution/tasks.py:288-290) — i.e. it would pass with the kwargs line deleted. The DB-attempt-owner fence is a distinct layer from the Redis claim fence covered by test_task_queue_preemption_rejects_stale_container_attempt, so it is worth proving correctly.
  - **How:** Persist `ContainerRecord`s for both TASK_QUEUE_CONTAINER_ID and CURRENT_TASK_QUEUE_CONTAINER_ID, pop with TASK_QUEUE_CONTAINER_ID so the Redis claim is held, then set the durable owner via `task.container_id = CURRENT_TASK_QUEUE_CONTAINER_ID` (replacing the `task.kwargs[...]` write at packages/execution/tests/test_taskqueue_service.py:619) before `isolated_services.tasks.save(task)`. Assert `current.container_id == CURRENT_TASK_QUEUE_CONTAINER_ID` instead of `current.kwargs["container_id"]` at packages/execution/tests/test_taskqueue_service.py:636.

### `packages/foundation/tests/test_boundaries.py`

- [x] `test_handler_loader_imports_module_and_nested_callable` — verdict: keep
- [x] `test_handler_loader_imports_file_and_rejects_non_callable` — verdict: keep

### `packages/foundation/tests/test_process.py`

- [x] `test_run_process_bounds_each_output_stream` — verdict: keep
- [x] `test_run_process_timeout_escalates_and_reaps_process_group` — verdict: keep
- [x] `test_managed_command_publishes_stdout_and_stderr_before_exit` — verdict: keep
- [x] `test_managed_command_keeps_diagnostic_tail_when_output_sink_fails` — verdict: keep
- [x] `test_managed_command_waits_for_large_output_sink_eof_drain` — verdict: keep
- [x] `test_managed_command_reports_output_pipe_that_does_not_reach_eof` — verdict: keep

### `packages/gateway/tests/test_gateway_api_auth_streaming.py`

- [x] `test_workspace_token_create_names_and_deletes` — verdict: keep
- [x] `test_first_run_bootstrap_is_explicit_and_routes_fail_closed` — verdict: keep
- [x] `test_agent_routes_use_service_owned_join_and_agent_tokens` — verdict: keep
- [x] `test_compute_gateway_projections_honor_admin_workspace_override` — verdict: keep
- [x] `test_gateway_raw_object_upload_uses_file_spool_and_download_redirect` — verdict: keep
- [x] `test_gateway_object_stream_requires_auth_and_exact_content_proof` — verdict: keep
- [x] `test_gateway_attach_and_agent_streams_emit_sse` — verdict: keep
- [x] `test_gateway_task_routes_do_not_cross_workspace_boundaries` — verdict: keep
- [x] `test_gateway_request_events_persist_only_server_errors` — verdict: keep

### `packages/gateway/tests/test_gateway_route_prewarm.py`

- [x] `test_route_prewarm_dials_route_writes_preface_emits_event_and_throttles` — verdict: keep
- [x] `test_route_prewarm_emits_error_with_peer_status` — verdict: keep
- [x] `test_thread_route_prewarm_runner_quiesces_before_close` — verdict: keep

### `packages/gateway/tests/test_gateway_settings.py`

- [x] `test_gateway_settings_keep_public_and_runtime_origins_independent` — verdict: keep
- [x] `test_gateway_settings_reject_non_origin_urls[]` — verdict: keep
- [x] `test_gateway_settings_reject_non_origin_urls[control.example.com]` — verdict: keep
- [x] `test_gateway_settings_reject_non_origin_urls[ftp://control.example.com]` — verdict: keep
- [x] `test_gateway_settings_reject_non_origin_urls[https://]` — verdict: keep
- [x] `test_gateway_settings_reject_non_origin_urls[https://user:secret@control.example.com]` — verdict: keep
- [x] `test_gateway_settings_reject_non_origin_urls[https://control.example.com/api]` — verdict: keep
- [x] `test_gateway_settings_reject_non_origin_urls[https://control.example.com?workspace=one]` — verdict: keep
- [x] `test_gateway_settings_reject_non_origin_urls[https://control.example.com#fragment]` — verdict: keep
- [x] `test_gateway_settings_reject_non_origin_urls[https://control.example.com:0]` — verdict: keep
- [x] `test_gateway_settings_reject_non_origin_urls[https://control.example.com:99999]` — verdict: keep

### `packages/gateway/tests/test_machine_lifecycle.py`

- [x] `test_machine_deletion_releases_provider_capacity_before_gateway_cleanup` — verdict: **update** — The invariant is worth keeping (paid provider capacity must be released before the machine record is removed, packages/gateway/src/gateway/machine_lifecycle.py:46-55), but the test does not state it. The two collaborators keep separate lists (packages/gateway/tests/test_machine_lifecycle.py:10, :25) and the assertions only check each list has one entry (same file:54-55), so the word 'before' in the test name is never asserted — reversing the two calls in machine_lifecycle.py:46-55 leaves the test green. It is also the only test of this service (grep of release_bound_internal_pool_machine / MachineLifecycleService finds no other test) yet never reaches the only decision the service makes, the malformed-id NotFoundError guard at machine_lifecycle.py:41-45.
  - **How:** Give both fakes a single shared ordered call log (e.g. one `calls: list[tuple[str, str]]` passed to both _Gateway and _ProviderCompute) and assert `calls == [("release", machine_id), ("delete", machine_id)]` so the ordering the name claims is actually proven, and extend the same test to assert NotFoundError for a non-UUID machine id so the guard at machine_lifecycle.py:41-45 is covered rather than a second delegation case.

### `packages/gateway/tests/test_pod_proxy_connections.py`

- [x] `test_connection_decrement_deletes_zero_keys_and_never_creates_negative_state` — verdict: keep
- [x] `test_connection_open_only_persists_an_existing_keep_warm_lock` — verdict: keep
- [x] `test_real_redis_connection_counters_are_atomic_and_leave_no_idle_keys` — verdict: keep
- [x] `test_real_redis_connection_open_cannot_recreate_a_deleted_keep_warm_lock` — verdict: keep
- [x] `test_pinned_direct_http_connect_uses_its_separate_dial_deadline` — verdict: keep

### `packages/gateway/tests/test_pool_capacity_reservation_guard.py`

- [x] `test_pool_scale_delegates_to_compute_while_capacity_owner_lock_is_held` — verdict: keep
- [x] `test_pool_scale_refuses_open_reservation_before_compute_mutation` — verdict: keep
- [x] `test_pool_scale_repair_refuses_active_reservation_while_capacity_is_observed` — verdict: **delete** — Fails gate 3: it is a matrix row that exercises no transition its sibling does not. The guard is `if desired_machines != 0 and desired_machines >= max(current.desired_machines, current.observed_machines): return` (packages/gateway/src/gateway/service.py:999-1003) followed by the reservation check at service.py:1004-1005. This test calls scale_pool(..., 0, ...) (packages/gateway/tests/test_pool_capacity_reservation_guard.py:293) with current desired=0/observed=1 (same file:281), so `desired_machines != 0` short-circuits to False and the current record's values are never read — byte-identical execution to test_pool_scale_refuses_open_reservation_before_compute_mutation (same file:234-266), which asserts the identical ConflictError, empty mutation_calls and identical guard.events list. The one row that does pin a distinct clause is test_pool_scale_zero_refuses_active_reservation_when_stored_capacity_is_zero (same file:304-336, max()==0, the only row that fails if the `desired_machines != 0` clause is dropped); that row is kept.
- [x] `test_pool_scale_zero_refuses_active_reservation_when_stored_capacity_is_zero` — verdict: keep
- [x] `test_pool_scale_zero_refuses_unassigned_pending_workspace_container` — verdict: keep
- [x] `test_pool_scale_zero_disables_owner_worker_before_compute_mutation` — verdict: keep
- [x] `test_pool_state_refuses_mismatched_durable_capacity_owner` — verdict: keep
- [x] `test_pool_scale_refuses_active_pool_container_before_compute_mutation[pending]` — verdict: keep
- [x] `test_pool_scale_refuses_active_pool_container_before_compute_mutation[running]` — verdict: keep
- [x] `test_pool_delete_refuses_open_capacity_reservation_without_mutating_owned_state` — verdict: keep
- [x] `test_public_delete_refuses_helm_owned_kubernetes_pool_before_mutation` — verdict: keep
- [x] `test_pool_delete_uses_durable_capacity_owner_for_guard_and_scheduler_state` — verdict: keep

### `packages/gateway/tests/test_stub_config.py`

- [x] `test_pod_checkpoint_readiness_is_retained_by_source_and_deployed_stubs` — verdict: **update** — The test's real subject — checkpoint readiness config surviving normalization, get_or_create_stub and deploy_stub into durable control-plane state — is worth keeping, but it carries a vacuous assertion. packages/gateway/tests/test_stub_config.py:57-59 asserts `f"{deployed.invoke_url}/state" == f"https://compute.example/pod/public/{deployed.stub_id}/8080/state"`, which is mechanically implied by the assertion on the line immediately above it (same file:56) — the same string with the same literal suffix appended to both sides. It can never fail independently and proves no production decision.
  - **How:** Delete the assertion at test_stub_config.py:57-59 and keep the invoke_url assertion at :56; leave the checkpoint-readiness assertions (:31-35, :61-65) unchanged.

### `packages/identity/tests/test_atomic_authorization_postgres.py`

- [x] `test_postgresql_authorization_claims_are_atomic` — verdict: keep
- [x] `test_postgresql_non_reusable_token_has_exactly_one_authenticated_claimant` — verdict: keep
- [x] `test_postgresql_offline_recovery_requires_stopped_control_plane_and_replays` — verdict: keep

### `packages/identity/tests/test_auth_rpc.py`

- [x] `test_rpc_retry_attempt_matrix_preserves_bounded_terminal_decisions` — verdict: **delete** — Fails gate 1: the functions under test are not current production behaviour. `plan_rpc_retry` (packages/identity/src/identity/rpc.py:293) and `plan_rpc_retry_attempt` (:240), together with `RpcRetryPlan` / `RpcRetryAttemptDecision` / `RpcRetryDecisionReason`, have no caller anywhere in the repository — a full-tree grep for `plan_rpc_retry` returns only rpc.py itself and this test file. Nothing in production ever produces one of these decisions, so no failure of this test can affect authorization, durability or any user-visible outcome; it is also exactly the 'generated commands or plans' shape the gate excludes. (The retry planner itself is dead production code and should go with the test; `sign_payload` / `verify_payload_signature` in the same module are live and are covered by packages/execution/tests/test_task_callbacks.py:16.)

### `packages/identity/tests/test_authorization_policy.py`

- [x] `test_auth_service_records_token_kind_and_checks_scopes` — verdict: keep
- [x] `test_expired_token_cannot_be_reactivated` — verdict: keep
- [x] `test_bootstrap_succeeds_once_and_never_reopens` — verdict: keep
- [x] `test_auth_service_cache_is_explicitly_shared_reset_and_closed` — verdict: keep
- [x] `test_auth_service_defaults_do_not_share_process_global_cache` — verdict: keep
- [x] `test_auth_service_authenticate_uses_prefix_candidates` — verdict: **delete** — Fails gates 1 and 2: it asserts an internal query strategy, not an authorization outcome. It monkeypatches `TokenRepository.list` to raise (packages/identity/tests/test_authorization_policy.py:174-188) and `identity.auth._verify_token` to record hashes (:184-189), then asserts `seen_hashes == [record.token_hash]` (:192) — i.e. that `authenticate` narrows through `repository.list_by_prefix(token[:10])` (packages/identity/src/identity/auth.py:880) rather than scanning. If that narrowing regressed, authentication would still return exactly the same record; only the number of PBKDF2 verifications would change, so the failure is a performance property, not authorization, security, data integrity or a public contract. The authentication outcome it wraps is already proven at :27 (kind/scope/single-use) and :60 (successful authenticate) in the same file.
- [x] `test_policy_decisions_cover_workspace_admin_and_restricted_tokens` — verdict: keep
- [x] `test_policy_decisions_cover_worker_machine_and_external_input` — verdict: keep
- [x] `test_disabled_tokens_are_rejected_by_policy` — verdict: keep
- [x] `test_public_token_api_enforces_exactly_one_single_use_request` — verdict: keep

### `packages/identity/tests/test_device_authorization.py`

- [x] `test_device_login_flow_approves_and_mints_workspace_token` — verdict: keep
- [x] `test_device_code_approval_requires_workspace_write_access` — verdict: keep
- [x] `test_device_codes_expire_and_are_pruned` — verdict: keep
- [x] `test_expired_device_code_claim_reports_expired_before_prune` — verdict: keep
- [x] `test_device_claim_rolls_back_consumption_when_token_insert_fails` — verdict: keep

### `packages/identity/tests/test_offline_admin_bootstrap.py`

- [x] `test_credential_publication_refuses_a_symlink_output` — verdict: keep
- [x] `test_service_credentials_cannot_consume_or_bypass_admin_claim` — verdict: keep
- [x] `test_bootstrap_retry_publishes_the_exact_committed_token_once` — verdict: keep
- [x] `test_configured_bootstrap_token_is_stable_and_only_its_hash_is_stored` — verdict: keep
- [x] `test_configured_bootstrap_token_mismatch_fails_closed` — verdict: keep
- [x] `test_configured_bootstrap_token_requires_canonical_token` — verdict: keep
- [x] `test_recovery_request_replay_is_idempotent_and_audited_once` — verdict: keep
- [x] `test_different_bootstrap_request_cannot_replay_committed_claim` — verdict: keep

### `packages/identity/tests/test_secret_encryption.py`

- [x] `test_secrets_encrypt_before_persistence_and_read_plaintext` — verdict: keep
- [x] `test_secret_ciphertext_is_workspace_bound` — verdict: keep
- [x] `test_secrets_are_workspace_scoped` — verdict: keep
- [x] `test_secret_plaintext_records_fail_closed_without_persistence_write` — verdict: keep
- [x] `test_secret_tampering_is_rejected` — verdict: keep
- [x] `test_malformed_secret_ciphertext_is_rejected[wssec:v1:!bad!]` — verdict: keep
- [x] `test_malformed_secret_ciphertext_is_rejected[wssec:v1:]` — verdict: keep
- [x] `test_secret_ciphertext_is_bound_to_its_stored_name` — verdict: keep
- [x] `test_secret_reads_do_not_rewrite_ciphertext_or_timestamps` — verdict: keep

### `packages/identity/tests/test_token_invalidation.py`

- [x] `test_local_cache_alone_serves_stale_token_without_invalidation_signal` — verdict: **delete** — Fails gates 1 and 2: it pins a configuration production never runs and asserts the weaker-security direction. Both services are built without `token_invalidation` (packages/identity/tests/test_token_invalidation.py:27-28), so `AuthService._invalidation()` (packages/identity/src/identity/auth.py:326) returns None; every production process configures it at startup — apps/api/src/api/control_runtime.py:174 and apps/scheduler/src/scheduler_app/runtime.py:103 — so no production request takes this path. The assertion is that a revoked token still authenticates from a warm 30s cache (:36); if production changed so that it were rejected, this test would fail while the system got strictly safer, which is the opposite of a material failure. The invariant that actually matters — revocation and admin-disable landing immediately across replicas — is proven at :39 and :54 in the same file, and the outage fallback at :146.
- [x] `test_revoked_token_rejected_immediately_across_replicas` — verdict: keep
- [x] `test_admin_disable_rejected_immediately_across_replicas` — verdict: keep
- [x] `test_every_validity_mutation_emits_invalidation` — verdict: keep
- [x] `test_redis_outage_bypasses_cache_and_falls_back_to_database` — verdict: keep

### `packages/identity/tests/test_websocket_tickets.py`

- [x] `test_ticket_is_hashed_short_lived_and_contains_no_bearer` — verdict: keep
- [x] `test_ticket_consumption_is_single_use_and_audience_bound` — verdict: keep
- [x] `test_failed_scope_and_revocation_checks_consume_ticket` — verdict: keep
- [x] `test_expired_token_and_ticket_scope_mismatch_are_terminal` — verdict: keep
- [x] `test_ticket_mint_rejects_wrong_workspace_and_insufficient_scope` — verdict: keep
- [x] `test_ticket_mint_removes_uncertain_write_and_hides_store_details` — verdict: keep
- [x] `test_real_redis_getdel_is_atomic_and_expiry_is_terminal` — verdict: keep

### `packages/identity/tests/test_workspace_settings.py`

- [x] `test_workspace_audit_cursor_round_trips_through_validated_contract` — verdict: keep

### `packages/images/tests/test_image_archive_cache_execution.py`

- [x] `test_restore_image_archive_from_content_cache_writes_validates_and_renames` — verdict: keep
- [x] `test_restore_image_archive_from_content_cache_cleans_up_read_and_validation_failures` — verdict: keep
- [x] `test_publish_image_archive_to_content_cache_stores_and_reports_failures` — verdict: keep
- [x] `test_load_image_archive_uses_local_ready_then_content_cache_then_source_fallback` — verdict: keep

### `packages/images/tests/test_image_base_digest.py`

- [x] `test_base_image_digest_credentials_never_enter_shared_cache` — verdict: keep
- [x] `test_base_image_digest_cache_hits_and_refreshes_expired_entries` — verdict: keep
- [x] `test_base_image_digest_cache_is_lru_bounded_under_arbitrary_tags` — verdict: keep
- [x] `test_base_image_digest_resolution_reports_missing_and_failed_inspect` — verdict: keep
- [x] `test_base_image_digest_cache_shares_concurrent_lookup` — verdict: keep
- [x] `test_base_image_digest_cache_shares_failed_concurrent_lookup` — verdict: keep

### `packages/images/tests/test_image_build_architecture.py`

- [x] `test_image_architecture_changes_cache_identity_and_scheduler_contract` — verdict: keep

### `packages/images/tests/test_image_build_container_execution.py`

- [x] `test_container_service_image_build_executor_preserves_v2_worker_failure` — verdict: keep
- [x] `test_container_service_image_build_executor_owns_terminal_log_shutdown` — verdict: keep
- [x] `test_container_service_image_build_executor_reports_unjoined_log_stream` — verdict: keep
- [x] `test_container_service_image_build_executor_does_not_fail_on_log_stream_error` — verdict: keep
- [x] `test_container_service_image_build_executor_emits_live_events` — verdict: keep
- [x] `test_container_service_image_build_executor_rejects_private_inputs_on_v1` — verdict: keep
- [x] `test_scheduler_image_build_executor_submits_waits_and_delegates` — verdict: keep
- [x] `test_scheduler_image_build_executor_reports_scheduler_error` — verdict: keep
- [x] `test_scheduler_image_build_executor_does_not_expose_submission_exception` — verdict: keep
- [x] `test_scheduler_image_build_executor_cancels_pending_request_after_address_timeout` — verdict: keep
- [x] `test_scheduler_image_build_executor_tombstone_drops_queued_request_after_timeout` — verdict: keep
- [x] `test_scheduler_image_build_executor_cancels_pending_request_when_credentials_cannot_stage` — verdict: keep
- [x] `test_scheduler_image_build_executor_defers_placement_to_the_workspace_policy` — verdict: keep
- [x] `test_scheduler_stages_and_cleans_private_build_credentials` — verdict: keep
- [x] `test_scheduler_private_input_validation_never_emits_secret_values` — verdict: keep
- [x] `test_scheduler_failure_reaches_real_image_service_and_cleans_pending_state_and_credentials` — verdict: keep

### `packages/images/tests/test_image_build_container_lifecycle.py`

- [x] `test_image_build_container_lifecycle_service_refreshes_ttl_and_cancels` — verdict: keep
- [x] `test_image_build_container_lifecycle_deletes_pending_state_on_pending_cancel` — verdict: keep
- [x] `test_redis_ttl_store_and_event_bus_deliver_expiry_and_stop` — verdict: **delete** — Fails gate 1 and 2: it asserts the internals of the test double, not production behaviour. Both subjects are pass-through adapters — RedisImageBuildContainerTtlStore.set_build_container_ttl is a single redis.set(key, "1", ex=ttl) (packages/images/src/images/lifecycle.py:238-245) and has_build_container_ttl a single redis.exists (lifecycle.py:247-248); EventBusImageBuildStopPublisher just forwards plan_stop_build_events to RedisEventBus.send (lifecycle.py:255-279). The load-bearing assertions are `60 in fake.expirations.values()` and `len(fake.published) == 1` (packages/images/tests/test_image_build_container_lifecycle.py:96, :99), which read tests/redis_fakes.FakeRedis attributes, so no real Redis key expiry, no durable transition and no user-visible outcome is proven — only that a thin adapter called the client it was constructed with. The lifecycle decisions that do matter (which cancel actions run, TTL failure becoming a Failed build) are already proven at their owner by test_image_build_container_lifecycle_service_refreshes_ttl_and_cancels (same file:24) and test_runtime_image_build_fails_when_lifecycle_start_errors (same file:102).
- [x] `test_runtime_image_build_fails_when_lifecycle_start_errors` — verdict: keep

### `packages/images/tests/test_image_build_container_scheduling.py`

- [x] `test_image_build_scheduling_failure_requires_failed_image_build_state` — verdict: keep
- [x] `test_unmodified_private_image_uses_ephemeral_credentials_during_build` — verdict: keep

### `packages/images/tests/test_image_build_credentials.py`

- [x] `test_registry_credentials_filter_and_classify_provider_credentials` — verdict: keep
- [x] `test_registry_provider_detection_rejects_lookalike_hosts` — verdict: keep
- [x] `test_cloud_registry_authfile_entries_use_supported_docker_credentials` — verdict: keep
- [x] `test_ephemeral_image_build_credentials_are_bound_one_time_and_expiring` — verdict: keep
- [x] `test_image_build_credential_lease_rejects_mismatched_registry_auth` — verdict: keep

### `packages/images/tests/test_image_build_diagnostics.py`

- [x] `test_failed_image_build_diagnostics_are_useful_bounded_and_sanitized` — verdict: keep

### `packages/images/tests/test_image_build_planning.py`

- [x] `test_image_control_rejects_unresolved_mutable_base` — verdict: keep
- [x] `test_image_control_rejects_oversized_context_before_download` — verdict: keep
- [x] `test_concurrent_equivalent_builds_share_one_durable_record` — verdict: keep
- [x] `test_stale_image_build_claim_is_failed_and_replaced_after_crash` — verdict: keep
- [x] `test_stale_owner_cannot_publish_after_claim_takeover` — verdict: keep
- [x] `test_stale_archive_candidate_cannot_replace_selected_winner` — verdict: keep
- [x] `test_archive_publication_rejects_head_integrity_mismatch[1025-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]` — verdict: keep
- [x] `test_archive_publication_rejects_head_integrity_mismatch[1024-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb]` — verdict: keep
- [x] `test_image_build_cleanup_plans_and_removes_build_directory` — verdict: keep
- [x] `test_image_control_service_delivers_build_secrets_without_persisting_values` — verdict: keep
- [x] `test_image_control_stream_uses_execution_terminal_after_durable_event_cleanup` — verdict: keep
- [x] `test_image_control_stream_uses_execution_terminal_after_durable_record_cleanup` — verdict: keep
- [x] `test_image_control_secret_version_invalidates_identity_and_inline_values_fail` — verdict: keep
- [x] `test_image_control_service_cancels_running_build_when_stream_closes` — verdict: keep
- [x] `test_runtime_image_build_preserves_executor_terminal_status_without_events` — verdict: keep
- [x] `test_runtime_image_build_start_and_cancel_are_persisted` — verdict: keep

### `packages/images/tests/test_image_log_streaming.py`

- [x] `test_image_build_log_stream_close_is_idempotent_after_completion` — verdict: keep
- [x] `test_image_build_log_stream_close_reports_incomplete_shutdown_once` — verdict: keep

### `packages/lazycloud/tests/test_deployment_configuration.py`

- [x] `test_sdk_transport_preserves_positive_concurrency_and_resolved_capacity` — verdict: keep
- [x] `test_sdk_rejects_zero_concurrency_before_transport` — verdict: keep

### `packages/lazycloud/tests/test_public_cli.py`

- [x] `test_task_list_filters_by_exact_app_id` — verdict: keep
- [x] `test_public_cli_opens_an_existing_container_shell_without_a_handler` — verdict: keep
- [x] `test_interactive_shell_rejects_json_output_before_creating_a_session` — verdict: keep
- [x] `test_secret_show_masks_secret_value_by_default` — verdict: keep
- [x] `test_volume_remote_path_parser_supports_plain_and_scheme_syntax` — verdict: keep
- [x] `test_volume_delete_without_tty_requires_yes_flag` — verdict: keep
- [x] `test_volume_delete_without_tty_reports_clean_json_error` — verdict: keep
- [x] `test_volume_delete_with_yes_flag_skips_confirmation` — verdict: keep

### `packages/lazycloud/tests/test_public_cli_client_codegen.py`

- [x] `test_public_cli_generated_client_returns_typed_endpoint_result` — verdict: keep

### `packages/lazycloud/tests/test_public_cli_cloud_connection.py`

- [x] `test_cloud_status_reports_disconnected_as_valid_state` — verdict: keep
- [x] `test_cloud_validate_json_is_clean_and_exits_nonzero_for_typed_failure` — verdict: keep
- [x] `test_cloud_disconnect_waits_for_automatic_removal` — verdict: keep
- [x] `test_cloud_disconnect_opens_only_terminal_recovery_action` — verdict: keep
- [x] `test_cloud_connect_requires_a_provider_subcommand_and_account_id` — verdict: keep

### `packages/lazycloud/tests/test_public_cli_compute.py`

- [x] `test_compute_status_routes_workspace_and_preserves_json_contract` — verdict: keep
- [x] `test_compute_policy_update_sends_revisioned_guardrails` — verdict: keep

### `packages/lazycloud/tests/test_public_pool_scale.py`

- [x] `test_pool_scale_cli_puts_durable_capacity_through_the_public_client` — verdict: keep
- [x] `test_pool_status_cli_reads_durable_state_without_mutation` — verdict: keep

### `packages/lazycloud/tests/test_sdk_app_deploy.py`

- [x] `test_app_deploy_forwards_source_root_to_function_deployment` — verdict: keep
- [x] `test_app_deploy_applies_placement_to_every_deployable_resource` — verdict: keep
- [x] `test_app_deploy_placement_only_changes_the_selected_resource` — verdict: keep

### `packages/lazycloud/tests/test_sdk_apps.py`

- [x] `test_resource_client_percent_encodes_path_identifiers` — verdict: keep
- [x] `test_resource_client_raises_typed_decode_error` — verdict: keep
- [x] `test_resource_client_preserves_request_failures[failure0]` — verdict: keep
- [x] `test_resource_client_preserves_request_failures[failure1]` — verdict: keep

### `packages/lazycloud/tests/test_sdk_artifact_remote.py`

- [x] `test_artifact_save_remote_chunks_file_and_returns_saved_metadata` — verdict: keep
- [x] `test_artifact_save_remote_packages_directories_and_empty_files` — verdict: keep
- [x] `test_output_from_pil_image_and_zip_helpers` — verdict: **delete** — The zip half duplicates test_artifact_save_remote_packages_directories_and_empty_files (packages/lazycloud/tests/test_sdk_artifact_remote.py:111-131), which already proves directory packaging through the production `save()` path (`results.zip`, namelist `["a.txt"]`). What is left asserts derived filename literals only: `zipped_path.name` from packages/lazycloud/src/lazycloud/abstractions/artifact.py:210-214 and the `from_pil_image` suffix from artifact.py:197-204 — implementation shape with no authorization, durability, or data-integrity consequence.

### `packages/lazycloud/tests/test_sdk_cli_output.py`

- [x] `test_output_channels_preserve_json_cleanliness_and_restore_human_state` — verdict: keep
- [x] `test_normalize_exception_preserves_safe_actionable_details[html]` — verdict: keep
- [x] `test_normalize_exception_preserves_safe_actionable_details[oversized]` — verdict: keep
- [x] `test_normalize_exception_preserves_safe_actionable_details[structured]` — verdict: keep
- [x] `test_normalize_exception_preserves_safe_actionable_details[bare]` — verdict: keep
- [x] `test_normalize_exception_preserves_safe_actionable_details[auth]` — verdict: keep
- [x] `test_normalize_exception_preserves_safe_actionable_details[timeout]` — verdict: keep
- [x] `test_normalize_exception_preserves_safe_actionable_details[bounded]` — verdict: keep
- [x] `test_normalize_exception_preserves_safe_actionable_details[empty]` — verdict: keep

### `packages/lazycloud/tests/test_sdk_client_handles.py`

- [x] `test_function_handle_async_remote_json_preserves_json_result_format` — verdict: keep
- [x] `test_function_handle_remote_json_decodes_deferred_task_result` — verdict: keep
- [x] `test_function_handle_remote_decodes_cloudpickled_bytes` — verdict: keep
- [x] `test_endpoint_handle_async_request_uses_request_payload` — verdict: keep

### `packages/lazycloud/tests/test_sdk_compute_placement.py`

- [x] `test_all_executable_app_resources_preserve_explicit_placement` — verdict: keep
- [x] `test_omitted_placement_remains_unresolved_for_workspace_default` — verdict: keep

### `packages/lazycloud/tests/test_sdk_config.py`

- [x] `test_sdk_profile_lifecycle_uses_sdk_config_owner` — verdict: keep
- [x] `test_sdk_profile_environment_overrides` — verdict: keep

### `packages/lazycloud/tests/test_sdk_contract_cases.py`

- [x] `test_sdk_decoders_consume_python_owned_contract_cases` — verdict: keep

### `packages/lazycloud/tests/test_sdk_control.py`

- [x] `test_resolve_control_client_config_precedence[worker-expected0]` — verdict: keep
- [x] `test_resolve_control_client_config_precedence[profile-expected1]` — verdict: keep
- [x] `test_resolve_control_client_config_precedence[explicit-expected2]` — verdict: keep
- [x] `test_resolve_control_client_config_precedence[packaged-expected3]` — verdict: keep
- [x] `test_resolve_control_client_config_precedence[endpoint-env-expected4]` — verdict: keep

### `packages/lazycloud/tests/test_sdk_function_endpoint.py`

- [x] `test_function_remote_stops_reading_after_terminal_stream_response` — verdict: keep
- [x] `test_function_remote_submits_child_task_inside_runtime_container` — verdict: keep
- [x] `test_function_import_guard_rejects_every_remote_invocation` — verdict: keep
- [x] `test_function_spawn_serializes_call_dependencies` — verdict: keep
- [x] `test_cron_decorates_raw_callable_and_registers_after_deploy` — verdict: keep
- [x] `test_cron_rejects_decorated_function_binding_helper` — verdict: keep
- [x] `test_function_remote_streams_status_logs_and_ignores_keepalives` — verdict: keep
- [x] `test_function_remote_distinguishes_none_result_from_incomplete_responses` — verdict: keep
- [x] `test_http_request_transport_timeout_follows_dispatch_wait_contract[endpoint-45-50.0]` — verdict: keep
- [x] `test_http_request_transport_timeout_follows_dispatch_wait_contract[endpoint-None-185.0]` — verdict: keep
- [x] `test_http_request_transport_timeout_follows_dispatch_wait_contract[endpoint-0-605.0]` — verdict: keep
- [x] `test_http_request_transport_timeout_follows_dispatch_wait_contract[asgi-45-50.0]` — verdict: keep
- [x] `test_endpoint_request_prefers_matching_serve_preview` — verdict: keep
- [x] `test_function_normalizes_mapping_task_policy_at_authoring_boundary` — verdict: keep
- [x] `test_endpoint_lifecycle_hooks_are_startup_only` — verdict: **update** — Half the test is a vacuous assertion. `assert not spec.lifecycle_hooks.on_success` (packages/lazycloud/tests/test_sdk_function_endpoint.py:757) can never fail: `Endpoint` exposes no `on_success` input (packages/lazycloud/src/lazycloud/abstractions/endpoint.py:219) and `spec()` builds hooks with `lifecycle_hooks(on_start=self.on_start)` only (endpoint.py:294), so the field is always empty. The remaining invariant — an `on_start` callable is exported as an importable `module:function` reference and reaches the spec — is worth keeping and is not proven for Endpoint anywhere else.
  - **How:** Drop `assert not spec.lifecycle_hooks.on_success`. Keep the `spec.lifecycle_hooks.on_start == (LIFECYCLE_HOOK_ONE_REF,)` assertion and rename the test to what it proves (on_start is exported as an importable reference). If the "startup only" claim is meant to be enforced, assert instead that `App(...).endpoint(on_success=...)` raises at the authoring boundary.
- [x] `test_explicit_schema_overrides_inferred_client_contract_inputs` — verdict: keep
- [x] `test_explicit_output_metadata_does_not_override_annotated_client_return` — verdict: keep
- [x] `test_client_contract_rejects_unresolved_annotations` — verdict: keep

### `packages/lazycloud/tests/test_sdk_handler_references.py`

- [x] `test_dotted_reference_uses_importable_module_name` — verdict: keep
- [x] `test_dotted_reference_derives_main_module_from_script_file` — verdict: keep
- [x] `test_dotted_reference_rejects_fileless_main_function` — verdict: keep
- [x] `test_dotted_reference_rejects_main_script_outside_current_directory` — verdict: keep
- [x] `test_source_root_reference_preserves_implicit_namespace_package` — verdict: keep
- [x] `test_source_root_reference_has_no_prefix_for_normal_import_root` — verdict: keep
- [x] `test_source_root_reference_rejects_loaded_handler_outside_bundle` — verdict: keep

### `packages/lazycloud/tests/test_sdk_http_transport.py`

- [x] `test_raw_transport_sends_a_bounded_readable_body_without_json_encoding` — verdict: keep
- [x] `test_object_upload_streams_with_progress_and_validates_response` — verdict: keep
- [x] `test_object_file_upload_streams_without_loading_a_second_copy` — verdict: keep
- [x] `test_object_upload_maps_http_failures_and_rejects_invalid_success` — verdict: keep

### `packages/lazycloud/tests/test_sdk_image_architecture.py`

- [x] `test_sdk_image_authoring_serializes_explicit_linux_architecture` — verdict: keep

### `packages/lazycloud/tests/test_sdk_image_build.py`

- [x] `test_sdk_image_build_request_preserves_credentials_without_leaking_spec_values` — verdict: keep
- [x] `test_sdk_image_with_docker_selects_only_supported_official_repositories` — verdict: **delete** — Asserts the literal text of a module constant, not a production decision. `with_docker()` only appends the fixed tuple `_DOCKER_INSTALL_COMMANDS` defined at packages/lazycloud/src/lazycloud/abstractions/image.py:45-84; the test re-states substrings of that constant (`. /etc/os-release; case "$ID" in debian|ubuntu)` at image.py:47, `download.docker.com/linux/$ID` at image.py:60/70, `VERSION_CODENAME` at image.py:66, and the apt package list at image.py:74-75). CLAUDE.md:134-136 forbids testing generated commands or constants, and nothing about whether Docker actually installs is proven without a real image build.
- [x] `test_sdk_image_build_context_rejects_symlink_to_outside_file` — verdict: keep
- [x] `test_sdk_image_build_request_resolves_env_credentials` — verdict: keep
- [x] `test_sdk_reads_google_service_account_file_before_transport` — verdict: keep
- [x] `test_sdk_rejects_invalid_google_service_account_file` — verdict: keep
- [x] `test_sdk_image_uv_project_maps_to_build_request_and_context` — verdict: keep
- [x] `test_sdk_image_uv_project_requires_lockfile` — verdict: keep
- [x] `test_sdk_image_build_revalidates_durable_identity_before_each_build` — verdict: keep

### `packages/lazycloud/tests/test_sdk_login_endpoint.py`

- [x] `test_login_endpoint_and_token_precedence[flag]` — verdict: keep
- [x] `test_login_endpoint_and_token_precedence[environment]` — verdict: keep
- [x] `test_login_endpoint_and_token_precedence[profile]` — verdict: keep
- [x] `test_login_endpoint_and_token_precedence[packaged]` — verdict: keep
- [x] `test_login_endpoint_and_token_precedence[token]` — verdict: keep
- [x] `test_login_accepts_environment_token_without_exposing_it` — verdict: keep

### `packages/lazycloud/tests/test_sdk_map.py`

- [x] `test_map_serializes_python_values_and_tracks_ttl` — verdict: keep
- [x] `test_map_get_and_delete_behave_like_mapping` — verdict: keep
- [x] `test_map_rejects_invalid_ttl_and_set_failures` — verdict: keep

### `packages/lazycloud/tests/test_sdk_pod.py`

- [x] `test_pod_lifecycle_resolution_raises_typed_error` — verdict: keep
- [x] `test_container_attaches_to_existing_container` — verdict: keep

### `packages/lazycloud/tests/test_sdk_pod_control.py`

- [x] `test_pod_file_routes_preserve_absolute_container_paths` — verdict: keep

### `packages/lazycloud/tests/test_sdk_queue.py`

- [x] `test_queue_serializes_python_values_and_uses_fifo_order` — verdict: keep
- [x] `test_queue_raises_typed_errors[put]` — verdict: keep
- [x] `test_queue_raises_typed_errors[pop]` — verdict: keep
- [x] `test_queue_raises_typed_errors[peek]` — verdict: keep
- [x] `test_queue_raises_typed_errors[empty]` — verdict: keep
- [x] `test_queue_size_failure_raises` — verdict: keep

### `packages/lazycloud/tests/test_sdk_sandbox_control.py`

- [x] `test_sandbox_memory_restore_does_not_prepare_a_new_stub` — verdict: keep
- [x] `test_sandbox_create_retries_only_typed_pending_readiness` — verdict: keep
- [x] `test_sandbox_create_does_not_retry_non_pending_http_failures[401]` — verdict: keep
- [x] `test_sandbox_create_does_not_retry_non_pending_http_failures[404]` — verdict: **delete** — Redundant matrix row. packages/lazycloud/src/lazycloud/abstractions/sandbox.py:1820 has a single branch (`if exc.status_code != 503`) covering every non-pending status, so this row re-runs exactly the code the [401] row already covers; there is no status-specific transition to protect (CLAUDE.md:101-102).
- [x] `test_sandbox_create_does_not_retry_non_pending_http_failures[409]` — verdict: **delete** — Redundant matrix row, same single branch at packages/lazycloud/src/lazycloud/abstractions/sandbox.py:1820. The retry-suppression and terminate-once cleanup it checks are already proven by the [401] row of the same test (packages/lazycloud/tests/test_sdk_sandbox_control.py:400-418).
- [x] `test_sandbox_create_timeout_preserves_state_and_cleans_up_once` — verdict: keep
- [x] `test_sandbox_create_transport_and_cleanup_failures_are_observable` — verdict: keep
- [x] `test_sandbox_constructor_rejects_invalid_declared_ports[0]` — verdict: keep
- [x] `test_sandbox_constructor_rejects_invalid_declared_ports[65536]` — verdict: keep
- [x] `test_sandbox_constructor_rejects_invalid_declared_ports[True]` — verdict: keep
- [x] `test_sandbox_url_operations_wrap_transport_failures_for_sync_and_async` — verdict: keep
- [x] `test_sandbox_prepare_emits_canonical_deployment_request` — verdict: keep

### `packages/lazycloud/tests/test_sdk_schema.py`

- [x] `test_image_schema_accepts_structural_image_objects_without_pillow` — verdict: keep

### `packages/lazycloud/tests/test_sdk_secret.py`

- [x] `test_secret_set_creates_then_updates_and_returns_records` — verdict: keep
- [x] `test_secret_create_update_delete_errors_are_typed` — verdict: keep
- [x] `test_secret_control_client_scopes_every_request_to_selected_workspace` — verdict: keep

### `packages/lazycloud/tests/test_sdk_serve.py`

- [x] `test_serve_preview_resolves_stub_url_and_stops_container_on_interrupt` — verdict: keep
- [x] `test_serve_preview_retries_attach_timeout` — verdict: keep
- [x] `test_serve_preview_uses_attach_event_stream` — verdict: **delete** — Pure mock transcript plus constructor-kwarg forwarding. Its only assertions (packages/lazycloud/tests/test_sdk_serve.py:210-211) are that `attach_to_container_events` was called for `ctr-serve` and that `attach_poll_seconds` was passed through to serve.py:210-212. The same attach path is already driven end-to-end by test_sdk_serve.py:146 (interrupt during attach), test_sdk_serve.py:172 (timeout retry) and test_sdk_serve.py:214 (attach failure stops the container), each of which asserts a real outcome.
- [x] `test_serve_preview_stops_container_when_attach_fails` — verdict: keep
- [x] `test_serve_workspace_syncer_writes_filtered_tree_through_gateway` — verdict: keep
- [x] `test_serve_workspace_syncer_can_seed_from_source_package_before_deltas` — verdict: keep
- [x] `test_serve_workspace_syncer_retries_until_worker_address_is_published` — verdict: keep
- [x] `test_serve_workspace_syncer_retries_until_container_service_is_ready` — verdict: keep

### `packages/lazycloud/tests/test_sdk_session_control.py`

- [x] `test_resource_get_preserves_non_not_found_failures[http-401-deployment]` — verdict: keep
- [x] `test_resource_get_preserves_non_not_found_failures[http-401-task]` — verdict: keep
- [x] `test_resource_get_preserves_non_not_found_failures[http-500-deployment]` — verdict: **delete** — Redundant matrix row. packages/lazycloud/src/lazycloud/session/deployment.py:358-360 branches only on `exc.status_code != 404`, so a 500 takes exactly the path the [http-401-deployment] row already exercises. The genuinely distinct paths (non-HttpApiError transport failure) are kept by the `transport-*` rows.
- [x] `test_resource_get_preserves_non_not_found_failures[http-500-task]` — verdict: **delete** — Redundant matrix row. packages/lazycloud/src/lazycloud/session/task.py:508-510 branches only on `exc.status_code != 404`, so a 500 re-runs the path the [http-401-task] row already covers.
- [x] `test_resource_get_preserves_non_not_found_failures[transport-deployment]` — verdict: keep
- [x] `test_resource_get_preserves_non_not_found_failures[transport-task]` — verdict: keep
- [x] `test_task_subscription_rejects_invalid_response` — verdict: keep
- [x] `test_client_upload_bytes_uses_authenticated_raw_stream` — verdict: keep
- [x] `test_client_upload_file_streams_path_and_reports_progress` — verdict: keep
- [x] `test_gateway_control_client_streams_attach_events` — verdict: keep
- [x] `test_task_result_validation_does_not_echo_response_payload` — verdict: keep
- [x] `test_completed_function_call_rejects_malformed_result` — verdict: keep
- [x] `test_task_async_wait_returns_task_result` — verdict: keep
- [x] `test_task_wait_retries_transient_read_failures[failure0]` — verdict: keep
- [x] `test_task_wait_retries_transient_read_failures[failure1]` — verdict: keep
- [x] `test_task_wait_propagates_http_error_responses_immediately` — verdict: keep
- [x] `test_function_call_gather_preserves_order_and_wait_options` — verdict: keep
- [x] `test_function_call_gather_can_return_exceptions_in_result_slots` — verdict: keep

### `packages/lazycloud/tests/test_sdk_shell.py`

- [x] `test_shell_creates_sessions_and_connect_plan` — verdict: keep
- [x] `test_shell_propagates_http_api_errors` — verdict: keep
- [x] `test_shell_control_client_uses_explicit_connect_plan_route` — verdict: keep
- [x] `test_interactive_shell_authenticates_resizes_streams_and_returns_exit_code` — verdict: keep
- [x] `test_interactive_shell_fails_closed_on_backend_error` — verdict: keep
- [x] `test_local_shell_terminal_restores_tty_state` — verdict: keep
- [x] `test_shell_session_repr_does_not_expose_password` — verdict: keep

### `packages/lazycloud/tests/test_sdk_signal.py`

- [x] `test_signal_uses_active_workspace_for_set_clear_and_monitor` — verdict: keep
- [x] `test_signal_monitor_without_handler_does_not_start` — verdict: keep
- [x] `test_signal_auto_starts_stream_monitor_during_user_code_import` — verdict: keep

### `packages/lazycloud/tests/test_sdk_source_sync.py`

- [x] `test_source_package_sync_collects_ignored_zip_once` — verdict: keep
- [x] `test_source_package_sync_reports_terminal_progress` — verdict: keep
- [x] `test_source_package_sync_preserves_canonical_module_prefix` — verdict: keep
- [x] `test_source_archive_prefix_changes_digest_and_cache_identity` — verdict: keep
- [x] `test_prefixed_source_package_round_trips_custom_type_with_one_module_identity` — verdict: keep
- [x] `test_source_archive_prefix_rejects_unsafe_paths[archive_prefix0]` — verdict: keep
- [x] `test_source_archive_prefix_rejects_unsafe_paths[archive_prefix1]` — verdict: keep
- [x] `test_source_archive_prefix_rejects_unsafe_paths[archive_prefix2]` — verdict: keep
- [x] `test_source_archive_prefix_rejects_unsafe_paths[archive_prefix3]` — verdict: keep
- [x] `test_deployment_prepare_syncs_source_and_sends_object_id` — verdict: keep
- [x] `test_deployment_prepare_keeps_handler_module_and_prefixes_nested_source_root` — verdict: keep
- [x] `test_deployment_object_upload_uses_extended_timeout_for_payload` — verdict: keep

### `packages/lazycloud/tests/test_sdk_taskqueue.py`

- [x] `test_task_queue_control_client_sends_named_serialized_invocation` — verdict: keep
- [x] `test_task_queue_lifecycle_hooks_flow_to_gateway_stub_request` — verdict: keep
- [x] `test_task_queue_serve_emits_prepare_progress_without_preconfigured_terminal` — verdict: keep
- [x] `test_task_queue_put_many_resolves_target_once_and_returns_batch` — verdict: keep
- [x] `test_task_queue_put_supports_versioned_target_and_handler_kwargs` — verdict: keep
- [x] `test_task_queue_put_prefers_matching_serve_preview` — verdict: keep
- [x] `test_task_queue_put_many_empty_does_not_prepare` — verdict: keep
- [x] `test_task_queue_put_many_raises_typed_error_on_enqueue_failure` — verdict: keep

### `packages/lazycloud/tests/test_sdk_values.py`

- [x] `test_json_safe_values_are_stored_as_inspectable_json` — verdict: keep
- [x] `test_non_json_values_fall_back_to_pickle_and_round_trip` — verdict: keep
- [x] `test_empty_payload_decodes_to_none` — verdict: keep

### `packages/lazycloud/tests/test_sdk_volume.py`

- [x] `test_volume_control_client_backed_file_operations` — verdict: keep
- [x] `test_volume_mounts_accept_exportables_and_reject_invalid_items` — verdict: keep
- [x] `test_volume_presigned_download_and_multipart` — verdict: keep
- [x] `test_volume_rejects_unsafe_paths_and_raises_typed_errors` — verdict: keep
- [x] `test_volume_delete_updates_ready_state` — verdict: keep
- [x] `test_cloud_bucket_config_rejects_partial_secret_references` — verdict: keep
- [x] `test_cloud_bucket_config_rejects_parent_prefix_segments` — verdict: keep

### `packages/networking/tests/test_backend_dialer.py`

- [x] `test_backend_route_dialer_connects_to_local_tcp_and_cleans_up` — verdict: keep

### `packages/networking/tests/test_settings.py`

- [x] `test_tailnet_control_rejects_partial_cleanup_credentials` — verdict: keep
- [x] `test_remote_provider_gate_reports_all_missing_security_requirements` — verdict: keep
- [x] `test_distinct_tailnet_tags_are_required` — verdict: **delete** — Already proven in the same file through the production gate. `TailnetControlSettings.validated_tags` raises for identical agent/control-plane tags at packages/networking/src/networking/settings.py:91-94, and `validate_provider_network_configuration` calls exactly that at packages/networking/src/networking/settings.py:160, with the resulting message asserted at packages/networking/tests/test_settings.py:55. The dedicated test at test_settings.py:62-69 re-proves the identical code path with no additional boundary, failing criterion 3.
- [x] `test_tailnet_tags_reject_invalid_values` — verdict: keep
- [x] `test_managed_runtime_requires_an_auth_key_when_enabled` — verdict: keep

### `packages/networking/tests/test_tailnet_control.py`

- [x] `test_issues_single_use_persistent_tagged_key_and_caches_oauth_token` — verdict: keep
- [x] `test_verifies_device_identity_hostname_authorization_and_tag` — verdict: keep
- [x] `test_rejects_device_that_does_not_match_machine_identity[hostname-other-machine-hostname]` — verdict: keep
- [x] `test_rejects_device_that_does_not_match_machine_identity[tags-value1-tag]` — verdict: keep
- [x] `test_rejects_device_that_does_not_match_machine_identity[authorized-False-not authorized]` — verdict: keep
- [x] `test_rejects_missing_and_duplicate_stable_node_ids` — verdict: keep
- [x] `test_finds_only_exact_tagged_generations_for_one_machine` — verdict: keep
- [x] `test_revoke_and_remove_are_idempotent_when_resources_are_absent` — verdict: keep
- [x] `test_machine_cleanup_revokes_key_before_exact_generation_reconciliation` — verdict: keep
- [x] `test_refreshes_oauth_token_once_after_api_authentication_failure` — verdict: keep
- [x] `test_classifies_upstream_failures[403-permission_denied-False]` — verdict: keep
- [x] `test_classifies_upstream_failures[404-not_found-False]` — verdict: keep
- [x] `test_classifies_upstream_failures[429-rate_limited-True]` — verdict: keep
- [x] `test_classifies_upstream_failures[503-upstream_unavailable-True]` — verdict: keep

### `packages/networking/tests/test_tailnet_runtime.py`

- [x] `test_sidecar_tailnet_runtime_verifies_status_without_login` — verdict: keep
- [x] `test_sidecar_tailnet_runtime_rejects_unauthenticated_status` — verdict: keep
- [x] `test_managed_tailnet_runtime_uses_auth_key_file_and_login_server` — verdict: keep
- [x] `test_managed_tailnet_runtime_refuses_stopped_identity_without_stable_node_id` — verdict: keep
- [x] `test_tailnet_runtime_retries_up_and_redacts_auth_key` — verdict: keep
- [x] `test_wait_for_peer_polls_until_tailnet_peer_is_reachable` — verdict: keep
- [x] `test_resolve_peer_host_prefers_online_duplicate_peer` — verdict: keep
- [x] `test_wait_for_peer_times_out_when_peer_is_missing` — verdict: keep
- [x] `test_repeated_missing_peer_refreshes_control_session_without_changing_identity[managed]` — verdict: keep
- [x] `test_repeated_missing_peer_refreshes_control_session_without_changing_identity[sidecar]` — verdict: keep
- [x] `test_failed_refresh_is_retried_by_later_start_without_auth_key[managed]` — verdict: keep
- [x] `test_failed_refresh_is_retried_by_later_start_without_auth_key[sidecar]` — verdict: keep
- [x] `test_new_runtime_recovers_stopped_reusable_identity_keylessly[managed]` — verdict: keep
- [x] `test_new_runtime_recovers_stopped_reusable_identity_keylessly[sidecar]` — verdict: keep
- [x] `test_cold_start_completes_refresh_interrupted_before_down[managed]` — verdict: keep
- [x] `test_cold_start_completes_refresh_interrupted_before_down[sidecar]` — verdict: keep
- [x] `test_peer_recovery_requires_sustained_misses_and_no_unrelated_active_traffic` — verdict: keep
- [x] `test_managed_tailnet_runtime_restart_reuses_authenticated_state_without_key` — verdict: keep
- [x] `test_managed_tailnet_runtime_waits_for_persisted_identity_to_load` — verdict: keep

### `packages/observability/tests/test_billing.py`

- [x] `test_task_count_usage_is_owner_scoped_and_idempotent` — verdict: keep
- [x] `test_usage_record_api_pages_more_than_one_thousand_records` — verdict: keep
- [x] `test_billing_report_uses_recorded_compute_cost_without_double_counting` — verdict: keep
- [x] `test_billing_prefers_direct_compute_only_within_the_same_metering_window` — verdict: keep
- [x] `test_billing_activity_uses_authoritative_metering_window_start` — verdict: keep
- [x] `test_managed_reservation_container_evidence_is_not_projected_twice` — verdict: keep
- [x] `test_billing_api_returns_compact_overview_and_lazy_workload_detail` — verdict: keep
- [x] `test_billing_api_resolves_typed_current_period_on_the_backend` — verdict: keep
- [x] `test_run_activity_is_attributed_without_becoming_a_billable_metric` — verdict: keep
- [x] `test_usage_price_catalog_is_validated_from_deployment_environment` — verdict: keep
- [x] `test_billing_csv_matches_authorized_report_and_preserves_all_sections` — verdict: keep
- [x] `test_billing_window_normalizes_to_utc_and_is_end_exclusive` — verdict: keep
- [x] `test_agent_node_usage_records_against_canonical_workspace_id` — verdict: keep

### `packages/observability/tests/test_container_log_ingestion.py`

- [x] `test_container_log_ingestion_service_supports_direct_durable_attribution` — verdict: keep
- [x] `test_container_log_runtime_attribution_uses_durable_ownership` — verdict: keep
- [x] `test_container_log_runtime_attribution_rejects_durable_task_conflict` — verdict: keep

### `packages/observability/tests/test_observability.py`

- [x] `test_invalid_telemetry_endpoint_is_rejected` — verdict: **delete** — Single-assert check that `plan_telemetry_endpoint` raises for a scheme-less string (packages/observability/src/observability/telemetry.py:209-213). It fails gate criterion 2: a rejected trace-exporter endpoint touches operability only — no authorization, security, data integrity, durability, concurrency, cleanup, public contract, or user-visible terminal outcome. The security-relevant part of the planner (http vs https -> TelemetryTransportSecurity, packages/observability/src/observability/telemetry.py:222-226) is not asserted at all, so the test is a bare validation-detail case on a pure helper.

### `packages/observability/tests/test_observability_logs_parity.py`

- [x] `test_redis_log_repository_applies_filter_combinations` — verdict: keep
- [x] `test_redis_log_stream_resumes_by_sequence_through_skipped_records` — verdict: keep
- [x] `test_redis_log_read_honors_clamp` — verdict: keep
- [x] `test_redis_event_repository_deletes_only_workspace_streams` — verdict: keep
- [x] `test_task_append_log_fans_out_to_app_scoped_live_stream` — verdict: keep
- [x] `test_api_log_history_and_stream_support_filters_wait_and_resume` — verdict: keep
- [x] `test_api_deployment_logs_resolve_deployment_to_owned_stream` — verdict: keep

### `packages/observability/tests/test_realtime_stream_retention.py`

- [x] `test_real_redis_single_and_batch_appends_bound_every_stream_and_cleanup` — verdict: keep
- [x] `test_real_redis_expired_cursors_clamp_or_raise_typed_conflict` — verdict: keep
- [x] `test_api_maps_expired_log_and_event_cursors_to_409` — verdict: keep

### `packages/observability/tests/test_repository_usage_events.py`

- [x] `test_event_prune_uses_short_telemetry_and_long_audit_retention` — verdict: keep
- [x] `test_worker_event_prune_deletes_aged_rows` — verdict: keep
- [x] `test_usage_repository_aggregation_groups_by_label_with_metadata_fallback` — verdict: **update** — The first half (packages/observability/tests/test_repository_usage_events.py:111-175) is a good repository-owner proof of billing aggregation with the label/metadata coalesce at packages/database/src/database/repositories/observability.py:430-440 — keep it. The second half (packages/observability/tests/test_repository_usage_events.py:177-212) is unrelated scope creep: it asserts the shape of `plan_usage_metric_emission` output (`plan.target == ":9191/metrics"`, `body["operation"]`, `body["subject"]`) — a generated plan, which the Do-not-test list names explicitly — and it is owned by `shared.usage`, not observability. Its one material assertion, credential masking in the serialized plan, is already proven at packages/observability/tests/test_usage_exporter.py:35-37.
  - **How:** Delete lines 177-212 (the `plan_usage_metric_emission` block) and the now-unused `SecretStr`, `UsageCollectorKind`, `UsageMetricOperation`, `UsageMetricsSinkSettings`, `plan_usage_metric_emission` imports at packages/observability/tests/test_repository_usage_events.py:13-23. Keep the aggregation assertions at lines 169-175 unchanged.

### `packages/observability/tests/test_settings.py`

- [x] `test_observability_settings_reject_incomplete_runtime_configuration[<lambda>-OpenMeter URL is required]` — verdict: keep
- [x] `test_observability_settings_reject_incomplete_runtime_configuration[<lambda>-managed billing endpoint is required]` — verdict: keep
- [x] `test_observability_settings_reject_incomplete_runtime_configuration[<lambda>-required managed billing cannot be disabled]` — verdict: keep
- [x] `test_observability_secrets_are_masked_in_settings_repr` — verdict: keep

### `packages/observability/tests/test_task_latency.py`

- [x] `test_task_latency_percentiles_and_cold_starts` — verdict: keep
- [x] `test_task_latency_scopes_by_stub_and_deployment` — verdict: keep

### `packages/observability/tests/test_usage_exporter.py`

- [x] `test_usage_exporter_masks_credentials_and_preserves_post_contract` — verdict: keep

### `packages/observability/tests/test_usage_query_performance.py`

- [x] `test_billing_report_projects_only_billable_evidence_with_constant_query_count` — verdict: **delete** — Its unique content is generated-SQL text and statement counts (`len(selects) == 3`, `"usage_records.metric IN" in ...`, `"GROUP BY" in ...` at packages/observability/tests/test_usage_query_performance.py:111-136, 160-163) — implementation shape and a performance property, neither of which is in gate criterion 2. Worse, packages/observability/tests/test_usage_query_performance.py:116 asserts `"json_extract" in usage_statement.lower()`, which is the SQLite-only branch of packages/database/src/database/repositories/observability.py:436-440; production runs PostgreSQL and emits `jsonb_extract_path_text`, so the assertion pins a shape production never produces. The behavioural residue (overview == report, workloads == report.workloads) is already proven at packages/observability/tests/test_billing.py:361-362 and 504-505, and again at packages/observability/tests/test_usage_query_performance.py:248 and 302.
- [x] `test_usage_summary_groups_and_filters_in_one_sql_statement` — verdict: **delete** — Unique content is `len(selects) == 1` and `"sum(usage_records.quantity)" in selects[0]` (packages/observability/tests/test_usage_query_performance.py:202-204) — generated SQL text and a query count, i.e. implementation shape with no authorization/integrity/durability consequence. The only behavioural assertion, label-filtered aggregation grouped by app (packages/observability/tests/test_usage_query_performance.py:205-208), is already proven at the cheaper repository owner by packages/observability/tests/test_repository_usage_events.py:159-175, which covers grouping by App and by Gpu including the metadata fallback.
- [x] `test_billing_window_projection_replaces_stable_record_contribution` — verdict: keep
- [x] `test_billing_ranges_use_metering_time_with_creation_fallback` — verdict: keep
- [x] `test_billing_projection_recomputes_shared_window_timestamp_after_replacement` — verdict: keep
- [x] `test_zero_cost_record_does_not_create_empty_billing_projection` — verdict: keep
- [x] `test_postgresql_billing_projection_serializes_two_writers` — verdict: keep
- [x] `test_postgresql_billing_projection_serializes_shared_window_writers` — verdict: keep

### `packages/operations/tests/test_retention.py`

- [x] `test_worker_retention_bounds_caches_and_preserves_active_images` — verdict: keep
- [x] `test_durable_retention_prunes_only_unreferenced_production_artifacts` — verdict: keep
- [x] `test_source_retention_preserves_cleanup_target_through_later_workspace_deletion` — verdict: keep
- [x] `test_retention_preserves_selected_archive_and_removes_loser` — verdict: keep
- [x] `test_checkpoint_retention_survives_empty_hot_state_index` — verdict: keep
- [x] `test_checkpoint_creation_and_restore_record_durable_retention_deadline` — verdict: keep
- [x] `test_source_and_image_candidates_recheck_references_before_physical_delete` — verdict: keep
- [x] `test_cleaned_image_tombstone_blocks_reference_until_republication` — verdict: keep
- [x] `test_duplicate_build_cleanup_preserves_shared_path_and_cache_key` — verdict: keep
- [x] `test_build_retention_age_starts_when_the_build_finishes` — verdict: keep
- [x] `test_image_cleanup_drains_high_cardinality_builds_in_bounded_batches` — verdict: keep
- [x] `test_resumed_image_cleanup_shares_one_build_budget_across_images` — verdict: keep
- [x] `test_build_resource_protection_uses_constant_query_count` — verdict: **delete** — The named invariant is `queries == 2` (packages/operations/tests/test_retention.py:1258) — a query-count/performance property, not one of the gate's material categories, and it pins internals of `ImageBuildRepository.protected_artifact_resources`. The behavioural residue — a build artifact path and cache key still referenced by a retained build must survive cleanup (packages/operations/tests/test_retention.py:1256-1257) — is already proven end-to-end at the service owner by packages/operations/tests/test_retention.py:997-1005, where the shared path and cache entry survive the sibling build's deletion (`shared_path.exists()`, `cache.list() == [cache_record]`, `build_paths_removed == 0`, `cache_entries_removed == 0`).
- [x] `test_artifact_reference_age_starts_when_the_build_finishes` — verdict: keep
- [x] `test_build_candidate_rechecks_status_before_deleting_physical_data` — verdict: keep
- [x] `test_source_cleanup_claim_survives_crash_and_rejects_new_reference` — verdict: keep
- [x] `test_slow_object_delete_does_not_block_unrelated_database_write` — verdict: keep
- [x] `test_object_delete_claim_does_not_block_another_workspace_location` — verdict: keep
- [x] `test_object_write_claim_blocks_delete_without_holding_database_transaction` — verdict: keep
- [x] `test_stale_object_write_claim_finalizes_matching_atomic_upload` — verdict: keep
- [x] `test_stale_object_operations_roll_back_missing_write_and_resume_delete` — verdict: keep

### `packages/operations/tests/test_task_rerun.py`

- [x] `test_rerun_preserves_original_opaque_function_invocation` — verdict: **update** — The invariant (an opaque cloudpickle invocation is replayed byte-identically) is real data integrity, but the test proves it against a mock transcript: every assertion reads `_RecordingInvoker.bodies[0]` (packages/operations/tests/test_task_rerun.py:88-94), the request the service handed to a stub invoker that fabricates a bare task at packages/operations/tests/test_task_rerun.py:31-36. Mock transcripts are on the Do-not-test list, and the real owner is available: production wires `TaskRerunService(core, function_invoker=function)` where `function` is `FunctionControlService` (apps/api/src/api/server/services.py:1340), and the composed instance is exposed as `isolated_services.task_rerun_service` (apps/api/src/api/server/services.py:545).
  - **How:** Drive the rerun through `isolated_services.task_rerun_service` (the production composition) and assert the durable result instead of the recorded request: `isolated_services.tasks.get(new_task.id).invocation == invocation` and that `.bytes_value()` equals `original_body`. Delete `_RecordingInvoker`/`_RerunFixture`/`_rerun_service` (packages/operations/tests/test_task_rerun.py:23-48) once no test needs them.
- [x] `test_rerun_copies_declared_dependency_edges` — verdict: **update** — Asserts only the mock transcript `fixture.invoker.bodies[0].dependencies` (packages/operations/tests/test_task_rerun.py:126-128) and never checks that the rerun task actually carries the copied edge, so a regression in how `FunctionControlService.function_invoke` persists `FunctionInvokeBody.dependencies` would not be caught. The invariant (declared DAG edges survive a rerun) is genuine data integrity and should be proven at the real owner, which is reachable via `isolated_services.task_rerun_service` (apps/api/src/api/server/services.py:545, wired at apps/api/src/api/server/services.py:1340).
  - **How:** Rerun through `isolated_services.task_rerun_service`, then read the persisted edges for the new task with `TaskDependencyRepository(session).list_for_task(new_task.id)` and assert `[edge.upstream_task_id] == [upstream.id]`, replacing the assertion on `invoker.bodies[0].dependencies` at packages/operations/tests/test_task_rerun.py:126-128.
- [x] `test_rerun_rejects_task_queue_tasks` — verdict: keep
- [x] `test_rerun_rejects_live_tasks` — verdict: keep
- [x] `test_rerun_scopes_to_workspace` — verdict: keep

### `packages/provider-clients/tests/test_provider_clients_aws_connections.py`

- [x] `test_aws_connection_composition_rejects_non_content_addressed_template_url` — verdict: keep
- [x] `test_aws_compute_catalog_only_includes_launchable_priced_region_types` — verdict: keep
- [x] `test_managed_validation_shares_only_regional_capacity_amis` — verdict: keep
- [x] `test_validation_rejects_obsolete_managed_connection_template` — verdict: keep
- [x] `test_cleanup_adapter_preserves_operation_and_exact_node_identity` — verdict: keep
- [x] `test_cleanup_adapter_maps_managed_stack_failure_to_customer_action` — verdict: keep

### `packages/provider-clients/tests/test_provider_network_factory.py`

- [x] `test_factory_rejects_record_gateway_different_from_canonical_origin` — verdict: keep
- [x] `test_registry_reads_one_durable_aws_snapshot_per_resolution` — verdict: keep

### `packages/provider-clients/tests/test_provider_settings.py`

- [x] `test_aws_connection_settings_reject_invalid_enabled_authority` — verdict: keep
- [x] `test_aws_capacity_settings_reject_partial_and_mutable_artifacts` — verdict: keep

### `packages/provider-clients/tests/test_registry_credentials.py`

- [x] `test_resolver_exchanges_source_aws_credentials_for_short_lived_ecr_auth` — verdict: keep
- [x] `test_resolver_rejects_ecr_lookalike_host_before_provider_auth` — verdict: keep

### `packages/providers/aws/tests/test_provider_aws_account_connection.py`

- [x] `test_replacement_creates_new_generation_without_mutating_active` — verdict: keep
- [x] `test_customer_quick_create_action_is_bound_to_exact_account_and_generation` — verdict: keep
- [x] `test_customer_cleanup_action_requires_the_exact_managed_stack` — verdict: keep
- [x] `test_template_owns_authorization_network_and_exact_connection_node_scope` — verdict: **update** — The least-privilege scoping of the customer-account CloudFormation role is worth proving, but most of this test is a snapshot of a static JSON asset plus assertions about removed capabilities. test_provider_aws_account_connection.py:236-248 asserts the exact set of template resource names; :252-257 asserts the exact Outputs set; :273-282 asserts one policy statement by whole-dict equality; :283-285, :286, :290, :338, :341-342 assert that DenySelfRevocation, RetirePredecessorStack, RetirePredecessorRole, Conditions, UseOwnedVpcForDependentCreates, compute-node-*, NodeRole and NodeInstanceProfile are absent - i.e. capability gates that production already removed. Those rows break on harmless template edits without any behaviour change.
  - **How:** Keep only the assertions that are security decisions with material blast radius in a customer account: NodeSecurityGroup has no SecurityGroupIngress and only the documented egress rule (:258-262); the cloud-pool:managed-by tag on the role and every network resource (:251, :263-266); the tag-scoped Conditions on RunTaggedInstanceResources, UseManagedInstanceLaunchResources, TagManagedInstancesOnLaunch and PassOwnedNodeRole (:322-336); and the sweep proving no statement grants any mutating network action (:297-321). Drop the resource-name and Outputs set equality, the whole-dict statement snapshot, and every 'removed Sid is absent' assertion - the live policy semantics are already enforced by validate_aws_account_connection_template_policy (account_connection.py:1225).
- [x] `test_template_policy_allows_only_the_exact_generation_stack` — verdict: **delete** — Fails gate 3. The test body (test_provider_aws_account_connection.py:346) is a bare call to validate_aws_account_connection_template_policy(aws_account_connection_template_bytes()) with no assertion. Production already runs exactly that call on every load of the template: account_connection.py:1223-1225 has aws_account_connection_template_identity() invoke validate_aws_account_connection_template_policy(payload) before returning. Every production consumer (provider_clients/aws_connections.py:311 and :570) and every sibling test that builds a planner (test_provider_aws_account_connection.py:72) therefore already executes this guard; a broken template fails loudly at the production owner, not only here.
- [x] `test_template_policy_rejects_the_old_self_delete_deny` — verdict: keep
- [x] `test_template_policy_rejects_access_to_another_stack` — verdict: keep
- [x] `test_bucket_access_policy_scopes_objects_and_write_actions_to_prefix` — verdict: keep
- [x] `test_bucket_access_control_reconciles_one_inline_node_role_policy` — verdict: keep
- [x] `test_validation_accepts_ready_stack_and_ensures_node_identity[CREATE_COMPLETE]` — verdict: keep
- [x] `test_validation_accepts_ready_stack_and_ensures_node_identity[UPDATE_COMPLETE]` — verdict: keep
- [x] `test_validation_rejects_stack_without_managed_network_outputs` — verdict: keep
- [x] `test_validation_rejects_non_ready_stack_states[REVIEW_IN_PROGRESS]` — verdict: **delete** — Matrix row that protects no distinct transition. Production decides this with a single frozenset membership test: _READY_AUTHORIZATION_STACK_STATUSES at account_connection.py:693 and the check at account_connection.py:804. All 21 rows drive the identical branch, so this row is a re-run of an inventory of third-party CloudFormation status constants. The invariant (non-ready stack rejected, no IAM role/profile created) stays proven by the retained *_COMPLETE rows, which are the only ones that fence a plausible suffix/prefix-match regression.
- [x] `test_validation_rejects_non_ready_stack_states[CREATE_IN_PROGRESS]` — verdict: **delete** — Same single branch as every other row: account_connection.py:804 tests membership in _READY_AUTHORIZATION_STACK_STATUSES (account_connection.py:693). An obviously-not-ready in-progress status adds no distinct high-risk transition over the retained rows.
- [x] `test_validation_rejects_non_ready_stack_states[CREATE_FAILED]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804). A *_FAILED status is never mistakable for ready; the row duplicates the retained coverage.
- [x] `test_validation_rejects_non_ready_stack_states[ROLLBACK_IN_PROGRESS]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804); no distinct transition beyond the retained ROLLBACK_COMPLETE row.
- [x] `test_validation_rejects_non_ready_stack_states[ROLLBACK_FAILED]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804); no distinct transition beyond the retained ROLLBACK_COMPLETE row.
- [x] `test_validation_rejects_non_ready_stack_states[ROLLBACK_COMPLETE]` — verdict: keep
- [x] `test_validation_rejects_non_ready_stack_states[DELETE_IN_PROGRESS]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804); no distinct transition beyond the retained DELETE_COMPLETE row.
- [x] `test_validation_rejects_non_ready_stack_states[DELETE_FAILED]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804). DELETE_FAILED handling that actually matters is proven separately at test_provider_aws_account_connection.py:1029 (test_delete_failed_requires_customer_action_for_exact_stack).
- [x] `test_validation_rejects_non_ready_stack_states[DELETE_COMPLETE]` — verdict: keep
- [x] `test_validation_rejects_non_ready_stack_states[UPDATE_IN_PROGRESS]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804); no distinct transition beyond the retained UPDATE_COMPLETE_CLEANUP_IN_PROGRESS and UPDATE_ROLLBACK_COMPLETE rows.
- [x] `test_validation_rejects_non_ready_stack_states[UPDATE_COMPLETE_CLEANUP_IN_PROGRESS]` — verdict: keep
- [x] `test_validation_rejects_non_ready_stack_states[UPDATE_FAILED]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804); no distinct transition.
- [x] `test_validation_rejects_non_ready_stack_states[UPDATE_ROLLBACK_IN_PROGRESS]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804); no distinct transition beyond the retained UPDATE_ROLLBACK_COMPLETE row.
- [x] `test_validation_rejects_non_ready_stack_states[UPDATE_ROLLBACK_FAILED]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804); no distinct transition beyond the retained UPDATE_ROLLBACK_COMPLETE row.
- [x] `test_validation_rejects_non_ready_stack_states[UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804). The prefix-match hazard it could fence is already covered by the retained UPDATE_COMPLETE_CLEANUP_IN_PROGRESS row.
- [x] `test_validation_rejects_non_ready_stack_states[UPDATE_ROLLBACK_COMPLETE]` — verdict: keep
- [x] `test_validation_rejects_non_ready_stack_states[IMPORT_IN_PROGRESS]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804); no distinct transition beyond the retained IMPORT_COMPLETE row.
- [x] `test_validation_rejects_non_ready_stack_states[IMPORT_COMPLETE]` — verdict: keep
- [x] `test_validation_rejects_non_ready_stack_states[IMPORT_ROLLBACK_IN_PROGRESS]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804); no distinct transition.
- [x] `test_validation_rejects_non_ready_stack_states[IMPORT_ROLLBACK_FAILED]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804); no distinct transition.
- [x] `test_validation_rejects_non_ready_stack_states[IMPORT_ROLLBACK_COMPLETE]` — verdict: **delete** — Same single branch as every other row (account_connection.py:693, :804). The '_COMPLETE that is not ready' hazard is already fenced by the retained ROLLBACK_COMPLETE, DELETE_COMPLETE, UPDATE_ROLLBACK_COMPLETE and IMPORT_COMPLETE rows.
- [x] `test_validation_rejects_role_that_does_not_enforce_external_id` — verdict: keep
- [x] `test_existing_role_uses_same_exact_node_identity_lifecycle` — verdict: keep
- [x] `test_managed_cleanup_deletes_node_identity_then_exact_stack_idempotently` — verdict: keep
- [x] `test_abandoned_pending_cleanup_waits_until_role_is_assumable` — verdict: keep
- [x] `test_delete_failed_requires_customer_action_for_exact_stack` — verdict: keep
- [x] `test_cleanup_rejects_invalid_cloudformation_operation_token` — verdict: keep

### `packages/providers/aws/tests/test_provider_aws_bootstrap_script.py`

- [x] `test_bootstrap_script_reports_phases_and_bounded_failures_without_gateway_installs` — verdict: **update** — The invariant (the node agent binary is digest-verified before it runs, and no failure path is unbounded) is material supply-chain security, but the test states it as literal substring matching on a generated shell script - the 'generated commands or plans' case the gate names. test_provider_aws_bootstrap_script.py:74-77 asserts exact one-liners including the precise curl flag string and the exact sha256sum/awk pipeline; :81-85 asserts the textual ordering of report_phase/ensure_docker/install-service inside the rendered body; :100-107 asserts exact case-arm text. Any harmless shell edit (adding --connect-timeout, renaming a local) fails the test without changing behaviour.
  - **How:** Keep the bash -n syntax proof (:65-68), which is the one assertion that catches a template that no machine could ever execute, and keep the two negative security facts stated behaviourally rather than textually: no /install/agent gateway route is used and no lazycloud-agent.service unit is written by this script (:72, :96). Replace the literal fragment and text-ordering assertions by driving the generated script the way the sibling test already does (test_provider_aws_bootstrap_script.py:130-150 sources the script and invokes one function under bash): invoke the download/verify function against a local file whose digest does not match AGENT_SHA256 and assert it fails and reports agent_download_failed, instead of asserting the shell text that implements it.
- [x] `test_shell_minted_sigv4_proof_matches_botocore_query_auth` — verdict: keep

### `packages/providers/aws/tests/test_provider_aws_capacity_images.py`

- [x] `test_capacity_image_sharing_only_shares_privately_owned_platform_images` — verdict: keep

### `packages/providers/aws/tests/test_provider_aws_ec2.py`

- [x] `test_aws_ec2_provision_machine_creates_atomically_tagged_idempotent_instance` — verdict: **update** — Idempotent provisioning is a real paid-resource invariant, but both distinguishing assertions are empty. test_provider_aws_ec2.py:38 asserts run_instances_kwargs["ClientToken"] == plan.client_token, which is a tautology: ec2.py:126 literally sets that key from the same self.client_token property (ec2.py:109-117), so the assertion compares the value to itself round-tripped through the plan's own serializer. test_provider_aws_ec2.py:39 asserts only that TagSpecifications is truthy, which proves nothing about the 'atomically tagged' claim in the test name even though those tags (provider.py:418-426) are what reconcile_machines later matches on.
  - **How:** Assert the two facts that actually carry the invariant. For idempotency, build two independent plans via provision_machine_plan for the same cluster/pool/operation_id and assert their client_token values are equal, and that a plan with a different operation_id yields a different token - that is what stops AWS creating a second paid instance on retry. For tagging, assert the TagSpecifications entry carries the machine-id and cluster-name tag keys with the expected values at creation time (AwsEc2TagKey.MachineId / AwsEc2TagKey.ClusterName), since test_aws_ec2_health_and_reconcile depends on those tags being present on the instance from birth.
- [x] `test_aws_ec2_health_and_reconcile` — verdict: keep
- [x] `test_aws_ec2_surviving_instance_or_volume_is_never_destroyed_storage` — verdict: keep
- [x] `test_aws_ec2_not_found_is_authoritative_machine_storage_absence` — verdict: keep

### `packages/providers/aws/tests/test_provider_aws_instance_catalog.py`

- [x] `test_instance_catalog_rejects_unsupported_capacity` — verdict: keep

### `packages/providers/aws/tests/test_provider_aws_managed_pool.py`

- [x] `test_managed_pool_ensure_is_idempotent_and_launches_into_the_stack_network` — verdict: keep
- [x] `test_managed_pool_storage_destruction_requires_exact_volume_absence` — verdict: keep
- [x] `test_pooled_provider_scales_and_reports_machine_infrastructure_health` — verdict: keep
- [x] `test_pooled_provider_requires_the_stack_provisioned_network` — verdict: keep
- [x] `test_pooled_provider_does_not_offer_unpriced_instance_types` — verdict: keep
- [x] `test_managed_pool_artifact_change_versions_template_and_updates_group` — verdict: keep
- [x] `test_managed_pool_delete_converges_after_asg_instance_cleanup` — verdict: keep
- [x] `test_managed_pool_describe_and_scale_use_the_owned_group` — verdict: **delete** — Fails gate 3: strict subset of the provider-level test in the same file. test_provider_aws_managed_pool.py:444-455 calls set_pool_capacity twice; the second call takes the branch at pooled_provider.py:135-143 (autoscaling_group_name already set), which is exactly provisioner.scale() followed by provisioner.describe(), and asserts updated.desired_machines == 2 plus autoscaling.update_count == 1. It also proves the same behaviour through the public PooledCapacityProvider boundary rather than the provisioner internals. The name claims an ownership check that this test never makes; the real ownership guard is proven at test_provider_aws_managed_pool.py:667 (test_managed_pool_rejects_same_named_group_without_ownership_tags).
- [x] `test_managed_pool_partial_failure_returns_last_durable_checkpoint` — verdict: keep
- [x] `test_managed_pool_rejects_same_named_group_without_ownership_tags` — verdict: keep
- [x] `test_managed_pool_maps_malformed_aws_inventory_to_typed_error` — verdict: keep

### `packages/providers/aws/tests/test_provider_aws_node_interruption.py`

- [x] `test_spot_interruption_monitor_reuses_imdsv2_token_when_no_notice_exists` — verdict: keep
- [x] `test_spot_interruption_monitor_refreshes_rejected_token_and_parses_notice` — verdict: keep
- [x] `test_spot_interruption_monitor_rejects_invalid_action_payload` — verdict: keep

### `packages/providers/aws/tests/test_provider_aws_provider_node_identity.py`

- [x] `test_identity_target_requires_connection_iam_scope` — verdict: keep
- [x] `test_identity_verifier_accepts_connection_and_managed_inventory` — verdict: keep
- [x] `test_identity_verifier_rejects_noncanonical_or_unscoped_urls[scheme]` — verdict: keep
- [x] `test_identity_verifier_rejects_noncanonical_or_unscoped_urls[host]` — verdict: keep
- [x] `test_identity_verifier_rejects_noncanonical_or_unscoped_urls[path]` — verdict: keep
- [x] `test_identity_verifier_rejects_noncanonical_or_unscoped_urls[fragment]` — verdict: keep
- [x] `test_identity_verifier_rejects_noncanonical_or_unscoped_urls[unknown-query]` — verdict: keep
- [x] `test_identity_verifier_rejects_noncanonical_or_unscoped_urls[action]` — verdict: keep
- [x] `test_identity_verifier_rejects_noncanonical_or_unscoped_urls[expiry]` — verdict: keep
- [x] `test_identity_verifier_rejects_noncanonical_or_unscoped_urls[session-token]` — verdict: keep
- [x] `test_identity_verifier_enforces_region_and_freshness_before_sts` — verdict: keep
- [x] `test_identity_verifier_requires_exact_role_and_instance_session` — verdict: keep
- [x] `test_identity_verifier_requires_managed_asg_inventory_membership` — verdict: keep
- [x] `test_identity_verifier_requires_atomic_durable_replay_claim` — verdict: keep
- [x] `test_identity_verifier_classifies_sts_failures[302-invalid_proof]` — verdict: keep
- [x] `test_identity_verifier_classifies_sts_failures[429-upstream_unavailable]` — verdict: keep
- [x] `test_identity_verifier_classifies_sts_failures[503-upstream_unavailable]` — verdict: keep
- [x] `test_identity_verifier_rejects_malformed_xml_and_transport_failure` — verdict: keep

### `packages/providers/aws/tests/test_provider_aws_provider_node_proof.py`

- [x] `test_ec2_identity_proof_uses_imdsv2_and_regional_sts` — verdict: keep
- [x] `test_ec2_identity_proof_rejects_region_mismatch_before_credentials` — verdict: keep
- [x] `test_ec2_identity_proof_requires_imdsv2_token` — verdict: keep

### `packages/runner/tests/test_endpoint_processes.py`

- [x] `test_endpoint_process_manager_starts_capacity_and_stops_group_on_child_failure` — verdict: keep
- [x] `test_function_endpoint_worker_joins_checkpoint_barrier_for_configured_capacity` — verdict: **delete** — Pure mock transcript of argument pass-through. The test calls the private `serve._run_function_endpoint_worker(..., 4)` (packages/runner/tests/test_endpoint_processes.py:121-127) with `serve.wait_for_checkpoint` monkeypatched (test_endpoint_processes.py:114-118) and its only assertion is `checkpoint_calls == [(True, 4)]` (test_endpoint_processes.py:129). Production merely relays that parameter — `packages/runner/src/runner/serve.py:433-457` passes `workers=workers` straight into `EndpointServeRunner`, which forwards it at packages/runner/src/runner/serve.py:221-223. No resulting state, side effect, error or cleanup is observed, so it fails gate criteria 1 and 2 and matches the explicit 'mock transcripts / constructor wiring' exclusion. The barrier behaviour it names is proven against the real filesystem handshake at packages/runner/tests/test_runner_checkpoints.py:8-42.

### `packages/runner/tests/test_function_invocation.py`

- [x] `test_runner_substitutes_only_declared_exact_dependency_markers` — verdict: keep
- [x] `test_runner_rejects_undeclared_and_unused_dependency_bindings` — verdict: keep
- [x] `test_runner_dependency_traversal_is_cycle_safe_and_depth_bounded` — verdict: keep

### `packages/runner/tests/test_json_result_serialization.py`

- [x] `test_task_queue_result_does_not_fabricate_empty_success_for_unsupported_value` — verdict: keep

### `packages/runner/tests/test_runner_checkpoints.py`

- [x] `test_runner_checkpoint_handshake_waits_and_loads_restored_identity` — verdict: keep

### `packages/runner/tests/test_runner_hot_reload.py`

- [x] `test_endpoint_runner_reload_evicts_mounted_user_code` — verdict: keep
- [x] `test_endpoint_runner_coerces_pydantic_input` — verdict: keep
- [x] `test_task_queue_runner_reload_evicts_handler_and_retry_types` — verdict: keep

### `packages/runner/tests/test_taskqueue_processes.py`

- [x] `test_task_queue_runner_waits_for_all_workers_before_polling` — verdict: keep
- [x] `test_task_queue_runner_config_rejects_invalid_worker_capacity[0]` — verdict: keep
- [x] `test_task_queue_runner_config_rejects_invalid_worker_capacity[-1]` — verdict: keep
- [x] `test_task_queue_runner_config_rejects_invalid_worker_capacity[not-an-integer]` — verdict: keep
- [x] `test_task_queue_process_manager_starts_capacity_and_stops_siblings_on_exit` — verdict: keep
- [x] `test_task_queue_child_restores_terminating_signal_handlers` — verdict: **delete** — Call-order assertion over a monkeypatched stdlib call. `signal.signal` is replaced at packages/runner/tests/test_taskqueue_processes.py:170 and the test asserts the exact ordered transcript `[(SIGINT, SIG_DFL), (SIGTERM, SIG_DFL)]` at test_taskqueue_processes.py:175-178; the ordering between the two is not a contract production owes (packages/runner/src/runner/taskqueue.py:699-700). Nothing about resulting state or a user-visible outcome is asserted, so criterion 1 fails, and criterion 2 is weak: an inherited parent handler cannot orphan a child because `TaskQueueProcessManager.stop` kills every process still alive after the 5s join deadline (packages/runner/src/runner/taskqueue.py:671-682).

### `packages/scheduler/tests/test_endpoint_autoscaling.py`

- [x] `test_endpoint_autoscaler_scales_up_from_active_dispatch_pressure` — verdict: keep
- [x] `test_endpoint_autoscaler_persists_scale_decisions_only_on_transition` — verdict: keep

### `packages/scheduler/tests/test_pod_autoscaling.py`

- [x] `test_pod_autoscaler_scales_immediately_idle_deployment_to_zero` — verdict: keep
- [x] `test_pod_autoscaler_replaces_running_records_without_live_scheduler_state` — verdict: keep
- [x] `test_pod_keep_warm_minus_one_is_durable_never_scale_to_zero` — verdict: keep
- [x] `test_pod_autoscaler_scales_down_only_idle_deployment_containers` — verdict: keep
- [x] `test_pod_last_proxy_disconnect_renews_idle_window_before_autoscaler_stop` — verdict: keep
- [x] `test_pod_proxy_finalization_is_idempotent_after_stub_deletion[2]` — verdict: keep
- [x] `test_pod_proxy_finalization_is_idempotent_after_stub_deletion[-1]` — verdict: keep
- [x] `test_pod_deployment_explicit_zero_scale_remains_zero_with_connections` — verdict: keep

### `packages/scheduler/tests/test_scheduler_agent_pool.py`

- [x] `test_agent_worker_pool_reconciles_connected_machine_and_capacity` — verdict: keep
- [x] `test_agent_worker_pool_excludes_machine_with_failed_typed_preflight` — verdict: keep
- [x] `test_agent_worker_pool_disables_stale_machine_worker` — verdict: keep

### `packages/scheduler/tests/test_scheduler_capacity_reservations.py`

- [x] `test_reservation_is_idempotent_per_request_and_reuses_compatible_capacity` — verdict: keep
- [x] `test_reservation_capacity_and_owner_identity_prevent_false_reuse` — verdict: keep
- [x] `test_capacity_service_requests_one_unit_then_reuses_the_durable_intent` — verdict: keep
- [x] `test_resolve_request_binds_and_fences_the_selected_capacity_owner` — verdict: keep
- [x] `test_terminal_retry_releases_stale_reservation_before_new_attempt` — verdict: keep
- [x] `test_unpinned_acquisition_fails_over_from_at_limit_pool_in_priority_order` — verdict: keep
- [x] `test_attached_pool_at_limit_remains_strict_without_fallback` — verdict: keep
- [x] `test_static_pool_persists_exact_desired_replica_before_scale` — verdict: keep
- [x] `test_fixed_pool_rejects_cross_workspace_and_oversized_capacity_requests` — verdict: keep
- [x] `test_static_pool_lost_scale_response_reconciles_without_second_increment` — verdict: keep
- [x] `test_disabled_static_pool_releases_capacity_owned_by_open_reservation` — verdict: keep
- [x] `test_placement_miss_transfers_capacity_to_dispatch_before_reconciliation` — verdict: keep
- [x] `test_final_dispatch_rechecks_owner_worker_after_scale_zero_mutation` — verdict: keep
- [x] `test_managed_provider_miss_persists_exact_unit_and_releases_only_owned_machine` — verdict: keep
- [x] `test_available_worker_registration_uses_reported_schedulable_capacity` — verdict: keep
- [x] `test_registration_proof_requires_the_reserved_preemptibility_class` — verdict: keep
- [x] `test_registration_expiry_calls_capacity_owner_release_and_records_failure` — verdict: keep
- [x] `test_cancellation_releases_exact_owned_capacity_after_last_allocation` — verdict: keep
- [x] `test_cancellation_after_registration_keeps_capacity_for_idle_drain` — verdict: keep
- [x] `test_reconcile_prunes_allocations_after_durable_container_owners_finish` — verdict: keep
- [x] `test_unconfirmed_cancellation_cleanup_remains_open_and_blocks_owner_mutation` — verdict: keep
- [x] `test_real_redis_dispatch_atomically_consumes_capacity_allocation` — verdict: keep
- [x] `test_incompatible_static_misses_claim_distinct_units_and_cancel_without_resurrection` — verdict: keep
- [x] `test_unclaimed_initial_pending_unit_is_claimed_once` — verdict: keep
- [x] `test_cpu_memory_and_gpu_exhaustion_prevent_false_compatible_reuse[cpu]` — verdict: keep
- [x] `test_cpu_memory_and_gpu_exhaustion_prevent_false_compatible_reuse[memory]` — verdict: keep
- [x] `test_cpu_memory_and_gpu_exhaustion_prevent_false_compatible_reuse[gpu]` — verdict: keep
- [x] `test_agent_and_disabled_pool_pending_workers_are_durably_reserved` — verdict: keep
- [x] `test_concurrent_compatible_misses_deduplicate_after_lock_retry` — verdict: keep
- [x] `test_reservation_repository_rejects_registered_state_regression` — verdict: keep
- [x] `test_capacity_owner_mutation_lock_renews_during_slow_owner_operation` — verdict: keep

### `packages/scheduler/tests/test_scheduler_compute_hooks.py`

- [x] `test_scheduler_compute_hooks_disable_machine_workers` — verdict: keep
- [x] `test_scheduler_compute_hooks_retire_provider_machine_hot_state` — verdict: keep

### `packages/scheduler/tests/test_scheduler_pool_drain.py`

- [x] `test_static_worker_pool_drain_uses_authoritative_owner_policy` — verdict: keep
- [x] `test_static_worker_pool_drain_requires_every_owned_worker_to_be_idle` — verdict: keep
- [x] `test_worker_pool_drain_scales_idle_static_pool_down_once` — verdict: keep
- [x] `test_scheduler_records_worker_pool_drain_events_and_metrics` — verdict: keep
- [x] `test_worker_pool_drain_holds_when_pool_has_active_containers` — verdict: keep
- [x] `test_worker_pool_drain_holds_while_capacity_allocation_is_open` — verdict: keep
- [x] `test_worker_pool_drain_terminates_idle_managed_provider_machine` — verdict: keep

### `packages/scheduler/tests/test_scheduler_pool_sizing.py`

- [x] `test_effective_headroom_counts_available_and_unclaimed_pending_then_allocations` — verdict: keep
- [x] `test_initial_floor_and_free_headroom_request_only_one_unit_per_reconcile` — verdict: keep
- [x] `test_pending_target_and_durable_cooldown_prevent_duplicate_scale_up` — verdict: keep

### `packages/scheduler/tests/test_scheduler_preemption.py`

- [x] `test_preemption_atomically_cordons_and_requeues_unstarted_work_once` — verdict: keep
- [x] `test_preemption_rejects_stale_worker_session_fence` — verdict: keep

### `packages/scheduler/tests/test_scheduler_redis_atomicity.py`

- [x] `test_script_transport_failure_propagates_without_non_atomic_fallback` — verdict: keep
- [x] `test_real_redis_claims_are_unique_and_expired_leases_recover` — verdict: keep
- [x] `test_real_redis_dispatch_and_cancellation_have_one_terminal_winner` — verdict: keep
- [x] `test_real_redis_blocking_take_delivers_payload_once` — verdict: keep
- [x] `test_real_redis_concurrency_reserve_and_release_are_bounded_and_idempotent` — verdict: keep
- [x] `test_real_redis_expired_token_owner_cannot_delete_replacement` — verdict: **delete** — Exact duplicate of the owning package's test. packages/scheduler/tests/test_scheduler_redis_atomicity.py:268-280 acquires a token lock, overwrites the key from a second client, then asserts TokenMismatch on the original owner's release and Released for the replacement. packages/coordination/tests/test_redis_coordination.py:107-116 performs the identical sequence against the same `coordination.token_lock` functions with real Redis. The lock primitive is owned by packages/coordination, not packages/scheduler, and the scheduler copy exercises no scheduler code at all (it imports nothing from `scheduler.*` for this case). Fails gate 3.

### `packages/scheduler/tests/test_scheduler_state_repository.py`

- [x] `test_container_exit_code_retains_typed_termination_reason` — verdict: keep
- [x] `test_scheduler_rejects_cron_payload_for_a_different_stub` — verdict: keep
- [x] `test_cron_failure_retries_same_run_then_persists_terminal_failure` — verdict: keep
- [x] `test_stopped_cron_deployment_cancels_due_retry_and_never_revives_it` — verdict: keep
- [x] `test_scheduler_tick_skips_cron_function_when_lock_is_held` — verdict: keep
- [x] `test_new_cron_version_removes_prior_schedule` — verdict: keep
- [x] `test_inactive_cron_deployment_never_enqueues` — verdict: keep
- [x] `test_scheduler_worker_repository_requeues_removed_worker_requests` — verdict: keep
- [x] `test_scheduler_worker_repository_requeues_expired_worker_requests[cleanup]` — verdict: keep
- [x] `test_scheduler_worker_repository_requeues_expired_worker_requests[list]` — verdict: keep
- [x] `test_scheduler_worker_repository_lifecycle_capacity_queue_and_image_pull_locks` — verdict: keep
- [x] `test_scheduler_request_claim_recovers_after_process_loss` — verdict: keep
- [x] `test_claim_dispatch_commit_survives_scheduler_crash_without_duplicate_delivery` — verdict: keep
- [x] `test_scheduler_worker_admin_service_lists_cordons_drains_and_removes_workers` — verdict: keep
- [x] `test_scheduler_container_repository_state_indexes_and_concurrency_release` — verdict: keep
- [x] `test_scheduler_container_request_service_queues_selects_and_dispatches` — verdict: **update** — Stale fixture after production moved. The shared helper `_request_service` (packages/scheduler/tests/test_scheduler_state_repository.py:224-252) never passes `capacity_reservations`, but every worker it registers carries `capacity_owner_id` (here packages/scheduler/tests/test_scheduler_state_repository.py:1284). Production now refuses final dispatch for an owned worker without reservation coordination and requeues with reason 'capacity-owner mutation coordination is unavailable' (packages/scheduler/src/scheduler/containers.py:770-779), so the assertion at line 1355 gets Waiting instead of Dispatched. Production is correct — the real scheduler process always wraps the service with capacity via `_container_requests_with_capacity` (apps/scheduler/src/scheduler_app/runtime.py:315-334) before calling dispatch_ready. The dispatch/selection invariant is worth keeping.
  - **How:** Give `_request_service` a `capacity_reservations` argument and wire the real coordinator the way the sibling suite already does — `CapacityReservationService(RedisCapacityReservationRepository(redis), tuple)` as at packages/scheduler/tests/test_scheduler_capacity_reservations.py:843-853 — so dispatch takes the same owner-mutation-lock path production takes. Assertions themselves need no change.
- [x] `test_scheduler_dispatch_clears_runtime_assignment_when_queueing_fails` — verdict: **update** — Same stale fixture. Worker registered with `capacity_owner_id` at packages/scheduler/tests/test_scheduler_state_repository.py:1405 while `_request_service` supplies no `capacity_reservations`, so packages/scheduler/src/scheduler/containers.py:773-779 short-circuits to Waiting and the assertion `result.status is SchedulerContainerDispatchStatus.Error` at line 1437 never runs against the real queue-failure rollback. The invariant (runtime assignment is cleared when the worker-queue commit throws) is material data-integrity coverage and must be preserved.
  - **How:** Wire `capacity_reservations=CapacityReservationService(RedisCapacityReservationRepository(redis), tuple)` through `_request_service` (pattern at packages/scheduler/tests/test_scheduler_capacity_reservations.py:843-853). The monkeypatched `dispatch_claimed_container_request` still raises inside the owner lease, so the Error + `assignments.cleared` assertions hold unchanged.
- [x] `test_scheduler_claim_dispatch_honors_cancellation_before_atomic_commit` — verdict: **update** — Same stale fixture: worker owner id at packages/scheduler/tests/test_scheduler_state_repository.py:1461, no `capacity_reservations` on the service, so dispatch returns Waiting and the Cancelled assertion at line 1510 is never reached. The cancellation-before-commit fence is a real terminal-state invariant that must stay covered.
  - **How:** Same fix as the sibling dispatch tests: pass a real `CapacityReservationService(RedisCapacityReservationRepository(redis), tuple)` into `_request_service`. Keep the monkeypatch that cancels inside `dispatch_claimed_container_request`; the ContainerRequestCancelledError still surfaces as Cancelled.
- [x] `test_worker_request_dequeue_is_atomic_without_the_worker_mutation_lock` — verdict: keep
- [x] `test_worker_request_blocking_pop_wakes_on_assignment_without_duplicate` — verdict: keep
- [x] `test_scheduler_dispatch_skips_durable_assignment_for_ephemeral_request` — verdict: **update** — Same stale fixture: `capacity_owner_id` on the worker at packages/scheduler/tests/test_scheduler_state_repository.py:1602 with no reservation coordinator, so packages/scheduler/src/scheduler/containers.py:773-779 requeues and the Dispatched assertion at line 1626 fails. The invariant — an ephemeral (image-build) request dispatches without recording a durable runtime assignment — is worth keeping.
  - **How:** Wire `capacity_reservations` into `_request_service` as at packages/scheduler/tests/test_scheduler_capacity_reservations.py:843-853; assertions unchanged.
- [x] `test_scheduler_container_cancellation_cannot_be_dispatched_or_requeued` — verdict: **update** — Same stale fixture (worker owner id at packages/scheduler/tests/test_scheduler_state_repository.py:1645). The second half of the test at line 1684 asserts `service.dispatch_ready(...)[0].dispatched`, which now returns Waiting because no capacity coordinator is wired (packages/scheduler/src/scheduler/containers.py:773-779). The cancellation-cannot-be-resurrected invariant is material cleanup coverage.
  - **How:** Wire `capacity_reservations` into `_request_service`; no assertion changes needed.
- [x] `test_scheduler_cancellation_removes_only_owned_backlog_and_preserves_fence` — verdict: keep
- [x] `test_workspace_container_state_cleanup_purges_only_owned_terminal_keys` — verdict: keep
- [x] `test_workspace_container_state_cleanup_purges_terminal_keys_after_state_deletion` — verdict: keep
- [x] `test_workspace_cleanup_discovers_ephemeral_container_after_state_deletion` — verdict: keep
- [x] `test_scheduler_cancellation_removes_assigned_request_and_all_indexes` — verdict: **update** — Same stale fixture (worker owner id at packages/scheduler/tests/test_scheduler_state_repository.py:1832). The precondition `dispatched[0].status is SchedulerContainerDispatchStatus.Dispatched` at line 1855 now yields Waiting, so the index-cleanup assertions at lines 1861-1870 never execute. Scoped index cleanup on cancellation is exactly the kind of cleanup obligation the gate wants proven.
  - **How:** Wire `capacity_reservations` into `_request_service`; assertions unchanged.
- [x] `test_scheduler_stopping_transition_is_atomic_with_dispatch_state_replacement` — verdict: keep
- [x] `test_scheduler_run_once_dispatches_when_pool_state_refresh_fails` — verdict: **update** — Same stale fixture (worker owner id at packages/scheduler/tests/test_scheduler_state_repository.py:1927). Assertion `result.container_dispatches[0].status is ... Dispatched` at line 1963 fails with Waiting for the coordination reason from packages/scheduler/src/scheduler/containers.py:778. The invariant — a failing pool-state refresh must not block container dispatch in `Scheduler.run_once` — is a real availability guarantee.
  - **How:** Wire `capacity_reservations` into `_request_service`; assertions unchanged.
- [x] `test_scheduler_dispatch_resumes_an_expired_claim_after_restart` — verdict: **update** — Same stale fixture (worker owner id at packages/scheduler/tests/test_scheduler_state_repository.py:1984). The recovery assertion at line 2018 fails with Waiting rather than Dispatched. Claim-lease expiry recovery after a scheduler restart is a durability/no-duplicate-delivery invariant that must be preserved.
  - **How:** Wire `capacity_reservations` into `_request_service`; assertions unchanged.
- [x] `test_scheduler_reconciles_confirmed_unrecoverable_sql_container` — verdict: keep
- [x] `test_scheduler_orphan_reconciliation_restores_pod_desired_capacity` — verdict: keep
- [x] `test_scheduler_ready_pop_and_worker_dispatch_are_atomic_under_parallel_schedulers` — verdict: **update** — Same stale fixture (worker owner id at packages/scheduler/tests/test_scheduler_state_repository.py:2185). Both parallel services lack a capacity coordinator, so neither dispatches and `len(dispatched) == 1` at line 2218 fails with 0. This is a concurrency invariant (exactly one winner across parallel schedulers) and is among the most valuable tests in the file.
  - **How:** Build both services in the list at packages/scheduler/tests/test_scheduler_state_repository.py:2175-2181 with their own `CapacityReservationService(RedisCapacityReservationRepository(<client>), tuple)`, mirroring packages/scheduler/tests/test_scheduler_capacity_reservations.py:843-853, so the owner mutation lock is genuinely contended. Keep the exactly-one-dispatch assertions.
- [x] `test_worker_capacity_reservation_and_enqueue_are_worker_lock_guarded` — verdict: keep
- [x] `test_scheduler_container_request_service_bounds_no_capacity_retries` — verdict: **update** — Stale expectation, not a fixture problem — no worker exists in this scenario, so dispatch reaches the terminal-failure path. Production now enriches the terminal reason via `_placement_failure_detail` at packages/scheduler/src/scheduler/containers.py:574-578, which returns 'retry-limit: no schedulable workers (pool selector <none>)' (packages/scheduler/src/scheduler/containers.py:1158-1160). The test still compares for equality against the bare enum value at packages/scheduler/tests/test_scheduler_state_repository.py:2328, 2333 and 2334. The retry-bound invariant is worth keeping; only the expectation is stale.
  - **How:** Change the three bare-equality assertions at packages/scheduler/tests/test_scheduler_state_repository.py:2328, 2333, 2334 to assert the reason starts with `SchedulerRetryReason.RetryLimit.value` (e.g. `failed[0].reason.startswith(...)`, `state.failure_reason.startswith(...)`, and match the container id plus prefix in `failure_handler.calls`). Leave the requeue-path assertion at line 2314 alone — only the terminal Fail path is enriched.
- [x] `test_scheduler_image_build_failure_persists_coordination_evidence_without_execution_callback` — verdict: **update** — Same stale expectation as above. Terminal reason is now 'retry-limit: no schedulable workers (pool selector <none>)' from packages/scheduler/src/scheduler/containers.py:1158-1160, while the test asserts bare equality at packages/scheduler/tests/test_scheduler_state_repository.py:2376 and 2382. The unique invariant here — image-build coordination evidence (build_id, image_id, upload capability) survives on the failed container state while the execution failure callback is deliberately not invoked (line 2383) — is worth keeping.
  - **How:** Relax the two reason comparisons at packages/scheduler/tests/test_scheduler_state_repository.py:2376 and 2382 to prefix checks against `SchedulerRetryReason.RetryLimit.value`. Leave the `failure_handler.calls == []` and image-build state assertions untouched.
- [x] `test_scheduler_container_request_service_waits_for_pending_worker_without_retry_count` — verdict: keep
- [x] `test_scheduler_container_request_service_reserves_quota_on_submit` — verdict: keep
- [x] `test_scheduler_container_repository_repairs_concurrency_counter_from_active_state` — verdict: keep
- [x] `test_concurrency_reservation_decisions_cover_repair_and_limits` — verdict: keep
- [x] `test_worker_network_ip_repository_preserves_ownership_invariants` — verdict: keep
- [x] `test_worker_pool_state_repository_persists_and_isolates_capacity_owners` — verdict: **delete** — Only asserts that RedisWorkerPoolStateRepository.set_state/get_state round-trips two snapshots keyed by distinct capacity owners (packages/scheduler/tests/test_scheduler_state_repository.py:2698-2702). The same repository round-trip and the stronger owner-isolation case — two owners sharing the identical `pool_name` 'shared-name' — are already proven in the same file at packages/scheduler/tests/test_scheduler_state_repository.py:2868-2876, which asserts `pool_states.get_state(owner_one) == states[owner_one]` and `pool_states.get_state(owner_two) == states[owner_two]` after a real SchedulerPoolStateService.refresh. Fails gate 3; nothing in the deleted test is unique.
- [x] `test_scheduler_pool_state_service_refreshes_worker_container_and_agent_snapshots` — verdict: keep
- [x] `test_scheduler_pool_state_service_isolates_same_display_name_by_capacity_owner` — verdict: keep

### `packages/scheduler/tests/test_taskqueue_autoscaling.py`

- [x] `test_task_queue_autoscaler_scales_up_from_queue_depth` — verdict: keep
- [x] `test_task_queue_autoscaler_expires_pending_work_before_scaling` — verdict: keep
- [x] `test_task_queue_autoscaler_reports_pending_container_metrics` — verdict: **update** — The invariant is worth proving but is stated as an observability assertion. With max_containers=1 and one container already pending, the second reconcile must not start a second container — that is the paid-resource guarantee. The test instead asserts only `result.pending_containers == 1` plus the `autoscaler_pending_containers` and `autoscaler_pressure_saturated` gauge values (packages/scheduler/tests/test_taskqueue_autoscaling.py:219-236) and never checks the `_Scheduler` recorder it installed at line 197, so the actual no-duplicate-start behaviour goes unasserted and a regression that starts a second container would still pass on the metric shape.
  - **How:** Assert the material outcome instead of the gauges: after the second reconcile, `len(scheduler.requests) == 1` (the recorder installed at packages/scheduler/tests/test_taskqueue_autoscaling.py:197) and `result.actions == []`, keeping `result.pending_containers == 1` as the decision signal. Drop the two `metric_value(...)` gauge assertions at lines 227-236.
- [x] `test_task_queue_autoscaler_records_scale_failure_capacity_metrics` — verdict: **delete** — Every assertion is an observability counter/gauge plus one constant action label: `[action.action for action in result.actions] == ["scale-up-failed"]` and the `autoscaler_scale_failures_total` / `autoscaler_no_worker_capacity_total` / `autoscaler_no_worker_capacity` metric values (packages/scheduler/tests/test_taskqueue_autoscaling.py:393-413). No durable state, container residue, cleanup, or user-visible outcome is asserted — with max_containers=1 the failure loop's `break` at packages/scheduler/src/scheduler/autoscaling.py:660-668 is never even exercised, so it does not prove runaway-start prevention either. Fails gate 2: a wrong metric label here affects no authorization, data-integrity, durability, concurrency, cleanup, or public-contract outcome.

### `packages/shared/tests/test_autoscaling_semantics.py`

- [x] `test_one_shot_pod_keep_warm_zero_drains_and_minus_one_stays_running` — verdict: keep

### `packages/shared/tests/test_common_stubs.py`

- [x] `test_stub_scoped_container_id_parser_accepts_current_prefixes` — verdict: keep
- [x] `test_stub_scoped_container_id_parser_rejects_function_and_invalid_ids` — verdict: keep
- [x] `test_stub_scoped_container_id_parser_preserves_hyphenated_suffixes_and_event_api` — verdict: keep

### `packages/shared/tests/test_common_utilities.py`

- [x] `test_url_builders_cover_path_host_public_and_ports` — verdict: keep
- [x] `test_deployment_spec_rejects_invalid_or_coerced_ports[0]` — verdict: keep
- [x] `test_deployment_spec_rejects_invalid_or_coerced_ports[65536]` — verdict: keep
- [x] `test_deployment_spec_rejects_invalid_or_coerced_ports[8080.5]` — verdict: keep
- [x] `test_deployment_spec_rejects_invalid_or_coerced_ports[8080]` — verdict: keep
- [x] `test_deployment_spec_rejects_invalid_or_coerced_ports[True]` — verdict: keep
- [x] `test_canonical_pod_proxy_urls_cover_path_and_host_modes` — verdict: keep
- [x] `test_pod_proxy_url_rejects_invalid_origin_and_port_matrix[-8080-None]` — verdict: **delete** — Row is an empty origin. pod_proxy_url delegates origin validation to normalize_http_origin (packages/shared/src/shared/urls.py:105), and the empty-origin rejection is already proven at the canonical owner in packages/shared/tests/test_urls.py:8. The row passes message=None so pytest.raises(match=None) asserts only that some ValueError escaped, adding no invariant beyond the cheaper owner test.
- [x] `test_pod_proxy_url_rejects_invalid_origin_and_port_matrix[lazycloud.dev-8080-None]` — verdict: **delete** — Missing-scheme origin. Already proven at the canonical owner: normalize_http_origin rejects a scheme-less value at packages/shared/src/shared/urls.py:39-40, covered by packages/shared/tests/test_urls.py:9 ('control.example.test'). pod_proxy_url only forwards the value (packages/shared/src/shared/urls.py:105).
- [x] `test_pod_proxy_url_rejects_invalid_origin_and_port_matrix[ftp://lazycloud.dev-8080-None]` — verdict: **delete** — Non-http scheme. Already proven at packages/shared/tests/test_urls.py:10 against normalize_http_origin, which is the function pod_proxy_url calls at packages/shared/src/shared/urls.py:105 (guard at urls.py:39-40).
- [x] `test_pod_proxy_url_rejects_invalid_origin_and_port_matrix[https://user:password@lazycloud.dev-8080-None]` — verdict: **delete** — Embedded-credentials origin. The security guard lives in normalize_http_origin at packages/shared/src/shared/urls.py:43-44 and is already proven at packages/shared/tests/test_urls.py:11. pod_proxy_url adds no credential handling of its own.
- [x] `test_pod_proxy_url_rejects_invalid_origin_and_port_matrix[https://lazycloud.dev/base-8080-None]` — verdict: **delete** — Path-bearing origin. Guard is normalize_http_origin at packages/shared/src/shared/urls.py:45-46, already proven at packages/shared/tests/test_urls.py:12.
- [x] `test_pod_proxy_url_rejects_invalid_origin_and_port_matrix[https://lazycloud.dev?mode=proxy-8080-None]` — verdict: **delete** — Query-bearing origin. Guard is normalize_http_origin at packages/shared/src/shared/urls.py:47-48, already proven at packages/shared/tests/test_urls.py:13.
- [x] `test_pod_proxy_url_rejects_invalid_origin_and_port_matrix[https://lazycloud.dev#proxy-8080-None]` — verdict: **delete** — Fragment-bearing origin. Guard is normalize_http_origin at packages/shared/src/shared/urls.py:47-48, already proven at packages/shared/tests/test_urls.py:14. (The remaining rows of this matrix are KEEP: the two whitespace rows and the three port rows exercise pod_proxy_url's own guards at urls.py:103-104 and urls.py:113-115 and are not proven elsewhere.)
- [x] `test_pod_proxy_url_rejects_invalid_origin_and_port_matrix[https://lazy cloud.dev-8080-None]` — verdict: keep
- [x] `test_pod_proxy_url_rejects_invalid_origin_and_port_matrix[ https://lazycloud.dev-8080-None]` — verdict: keep
- [x] `test_pod_proxy_url_rejects_invalid_origin_and_port_matrix[https://lazycloud.dev-0-between 1 and 65535]` — verdict: keep
- [x] `test_pod_proxy_url_rejects_invalid_origin_and_port_matrix[https://lazycloud.dev-65536-between 1 and 65535]` — verdict: keep
- [x] `test_pod_proxy_url_rejects_invalid_origin_and_port_matrix[https://lazycloud.dev-True-between 1 and 65535]` — verdict: keep
- [x] `test_validation_network_prefix_and_shell_quote` — verdict: keep
- [x] `test_managed_command_wait_poll_and_terminate` — verdict: **delete** — Fails gate 1 and gate 3. It exercises foundation-owned process helpers (foundation.process, packages/foundation/src/foundation/process.py:203-271) from packages/shared's suite, though packages/shared/CLAUDE.md:3-4 scopes shared to backend-free boundary contracts. The behaviour it asserts is already proven at the real owner: start_managed_command + poll()-is-None-while-running + terminate() returning captured stdout/stderr at packages/foundation/tests/test_process.py:96-127, and .wait(timeout_seconds=...) returning a completed result with bounded output at packages/foundation/tests/test_process.py:130-148 and :151-172. The only residue is wait(timeout_seconds=0.01) raising ManagedCommandStillRunning, which is a two-line passthrough of subprocess.Popen.wait's TimeoutExpired (packages/foundation/src/foundation/process.py:240-244) - a stdlib default, not a shared contract. It is also the slowest test in the suite (0.13s) because of a real subprocess plus time.sleep(0.1), making it host-timing dependent, and it leaves a dead helper class _CommandStateContract at packages/shared/tests/test_common_utilities.py:37-38 that no test references.

### `packages/shared/tests/test_execution_contracts.py`

- [x] `test_task_json_fields_reject_unencoded_python_values` — verdict: keep
- [x] `test_http_task_payload_round_trips_nested_json_and_query_values` — verdict: keep
- [x] `test_http_task_payload_rejects_invalid_or_non_object_json[not-json]` — verdict: keep
- [x] `test_http_task_payload_rejects_invalid_or_non_object_json[[]]` — verdict: keep
- [x] `test_http_task_payload_rejects_invalid_or_non_object_json[null]` — verdict: keep

### `packages/shared/tests/test_fleet_contracts.py`

- [x] `test_compute_pool_capacity_policy_rejects_ambiguous_ownership_and_shape` — verdict: keep
- [x] `test_compute_pool_capacity_owner_id_is_frozen` — verdict: keep
- [x] `test_container_and_cron_records_preserve_terminal_state_and_timestamps` — verdict: keep
- [x] `test_provider_config_accepts_only_recursive_json_values` — verdict: keep

### `packages/shared/tests/test_gpu_contract.py`

- [x] `test_gpu_normalization_preserves_no_gpu_any_and_overlapping_aliases` — verdict: **update** — The alias-normalization assertions are worth keeping (normalize_gpu_type drives placement matching, packages/shared/src/shared/gpu.py:81-102, and overlapping prefixes such as A10/A10G and L40/L40S and RTX6000/RTX6000Ada depend on the ordered alias table at gpu.py:45-78). But the module opens with CANONICAL_GPU_VALUES at packages/shared/tests/test_gpu_contract.py:3-35, a hand-copied literal inventory of every GpuType member from packages/shared/src/shared/gpu.py:11-42, which no test in the file references. It is dead weight and exactly the 'constants / provider inventory fields' the gate forbids: it will silently rot every time the enum gains a card.
  - **How:** Delete the unused CANONICAL_GPU_VALUES block at packages/shared/tests/test_gpu_contract.py:3-35. Keep the normalize_gpu_type assertions at lines 38-48 unchanged.
- [x] `test_gpu_normalization_keeps_unknown_provider_hardware_as_reported` — verdict: keep

### `packages/shared/tests/test_http_json_boundaries.py`

- [x] `test_http_json_contracts_reject_arbitrary_python[GetOrCreateStubRequest-payload0]` — verdict: keep
- [x] `test_http_json_contracts_reject_arbitrary_python[SyncContainerWorkspaceBody-payload1]` — verdict: keep
- [x] `test_http_json_contracts_reject_arbitrary_python[CronJobResponse-payload2]` — verdict: keep
- [x] `test_http_json_contracts_reject_arbitrary_python[TaskResponse-payload3]` — verdict: keep
- [x] `test_http_json_contracts_reject_arbitrary_python[StubConfigUpdateRequest-payload4]` — verdict: keep
- [x] `test_discriminated_results_pages_bytes_and_timestamps_are_precise` — verdict: **update** — The test bundles four unrelated assertions and two fail the gate. The FunctionResultPayload discriminated-union check (lines 84-95) is worth keeping: it pins the wire tag 'encoding' and the 'version': 1 envelope that runner and SDK both consume. But packages/shared/tests/test_http_json_boundaries.py:101 asserts utc_now().tzinfo is UTC, which only re-states datetime.now(UTC) inside packages/shared/src/shared/timestamps.py - a stdlib default, not a boundary contract. And line 97 pins the model_dump shape of RepositoryPage, an internal repository-layer page (packages/shared/src/shared/pagination.py:45-48) that is not a public JSON payload; the public list contract is {data, next} per CLAUDE.md:65-66, so this asserts implementation shape of a non-boundary model.
  - **How:** Drop the utc_now().tzinfo assertion at line 101 and the RepositoryPage.model_dump shape assertion at lines 96-97. Keep the FunctionResultPayload discriminated-union assertions (lines 84-95) and the EncodedBytesBody base64 round trip (lines 99-100), which are real wire contracts.
- [x] `test_sandbox_exposed_port_rejects_out_of_range_and_coerced_values[0]` — verdict: keep
- [x] `test_sandbox_exposed_port_rejects_out_of_range_and_coerced_values[65536]` — verdict: keep
- [x] `test_sandbox_exposed_port_rejects_out_of_range_and_coerced_values[True]` — verdict: keep
- [x] `test_sandbox_exposed_port_rejects_out_of_range_and_coerced_values[8080.0]` — verdict: keep
- [x] `test_sandbox_exposed_port_rejects_out_of_range_and_coerced_values[8080]` — verdict: keep
- [x] `test_canonical_worker_and_pool_views_preserve_nominal_json_contracts` — verdict: **update** — As written this is a tautology: it validates WorkerListResponse from a literal dict then asserts response.workers[0].id == 'worker-1' and that model_dump echoes machine_id and labels back (packages/shared/tests/test_http_json_boundaries.py:135-137). Every assertion re-states the test's own input, so it proves only that pydantic stores fields. It carries no serialization-loss, validation, or rejection evidence - contrast the genuine round-trip form used at packages/shared/tests/test_fleet_contracts.py:95-96. The real risk on these two wire views (datetime and closed-enum serialization on created_at/updated_at/status/capacity_owner_kind, per packages/shared/src/shared/http/CLAUDE.md:4) is left unasserted.
  - **How:** Replace the field-echo assertions with a real round-trip that would catch information loss, e.g. assert WorkerListResponse.model_validate_json(response.model_dump_json()) == response and PoolResponse.model_validate_json(pool.model_dump_json()) == pool, so datetime and enum serialization on created_at/status/capacity_owner_kind/capacity_owner_source is actually covered. If that is not wanted, delete the test - the current assertions protect nothing.

### `packages/shared/tests/test_http_transport_tls.py`

- [x] `test_http_ssl_context_keeps_strict_verification_and_adds_portable_roots` — verdict: keep
- [x] `test_http_channel_uses_its_strict_ssl_context` — verdict: keep
- [x] `test_http_channel_uses_the_shared_no_response_network_error` — verdict: keep

### `packages/shared/tests/test_identity_contracts.py`

- [x] `test_identity_records_round_trip_nested_json_without_default_loss` — verdict: keep
- [x] `test_identity_json_boundaries_reject_non_json_metadata_and_storage_config` — verdict: keep
- [x] `test_concurrency_limits_require_positive_capacity_and_nonnegative_usage` — verdict: keep

### `packages/shared/tests/test_image_building_credentials.py`

- [x] `test_registry_host_for_image_uses_docker_reference_rules[ubuntu:24.04-docker.io]` — verdict: keep
- [x] `test_registry_host_for_image_uses_docker_reference_rules[library/ubuntu:24.04-docker.io]` — verdict: keep
- [x] `test_registry_host_for_image_uses_docker_reference_rules[docker://ghcr.io/Acme/app:latest-ghcr.io]` — verdict: keep
- [x] `test_registry_host_for_image_uses_docker_reference_rules[oci://localhost:5000/team/app-localhost:5000]` — verdict: keep
- [x] `test_registry_host_for_image_uses_docker_reference_rules[registry.example.com:5443/team/app@sha256:abc-registry.example.com:5443]` — verdict: keep
- [x] `test_registry_host_for_image_uses_docker_reference_rules[https://REGISTRY.EXAMPLE.COM/team/app-registry.example.com]` — verdict: keep
- [x] `test_registry_host_for_image_uses_docker_reference_rules[-]` — verdict: keep
- [x] `test_registry_host_normalization_is_shared_by_host_comparison` — verdict: keep
- [x] `test_parse_ecr_registry_accepts_complete_aws_hosts[https://123456789012.dkr.ecr.us-east-1.amazonaws.com/team/app-123456789012-us-east-1-123456789012.dkr.ecr.us-east-1.amazonaws.com]` — verdict: keep
- [x] `test_parse_ecr_registry_accepts_complete_aws_hosts[123456789012.dkr.ecr-fips.us-gov-west-1.amazonaws.com:443-123456789012-us-gov-west-1-123456789012.dkr.ecr-fips.us-gov-west-1.amazonaws.com]` — verdict: keep
- [x] `test_parse_ecr_registry_accepts_complete_aws_hosts[123456789012.dkr.ecr.cn-north-1.amazonaws.com.cn/repository-123456789012-cn-north-1-123456789012.dkr.ecr.cn-north-1.amazonaws.com.cn]` — verdict: keep
- [x] `test_parse_ecr_registry_rejects_noncanonical_hosts[123456789012.dkr.ecr.us-east-1.amazonaws.com.evil.test]` — verdict: keep
- [x] `test_parse_ecr_registry_rejects_noncanonical_hosts[12345678901.dkr.ecr.us-east-1.amazonaws.com]` — verdict: keep
- [x] `test_parse_ecr_registry_rejects_noncanonical_hosts[public.ecr.aws]` — verdict: keep
- [x] `test_parse_ecr_registry_rejects_noncanonical_hosts[123456789012.dkr.ecr.us_east_1.amazonaws.com]` — verdict: keep
- [x] `test_parse_ecr_registry_rejects_noncanonical_hosts[123456789012.dkr.ecr.us-east-1.amazonaws.com:4444]` — verdict: keep
- [x] `test_parse_ecr_registry_rejects_noncanonical_hosts[https://user:secret@123456789012.dkr.ecr.us-east-1.amazonaws.com]` — verdict: keep
- [x] `test_registry_credential_inputs_resolve_environment_and_secret_names` — verdict: keep

### `packages/shared/tests/test_mounts.py`

- [x] `test_mount_prefix_normalization_is_deterministic` — verdict: keep
- [x] `test_mount_prefix_rejects_parent_segments` — verdict: keep

### `packages/shared/tests/test_process_liveness.py`

- [x] `test_beat_creates_parent_and_refreshes_mtime` — verdict: keep
- [x] `test_freshness_against_reference_clock` — verdict: keep
- [x] `test_check_command_exit_codes` — verdict: keep

### `packages/shared/tests/test_serialization.py`

- [x] `test_to_json_value_rejects_unsafe_or_unsupported_values[numeric]` — verdict: keep
- [x] `test_to_json_value_rejects_unsafe_or_unsupported_values[set]` — verdict: keep
- [x] `test_to_json_value_rejects_unsafe_or_unsupported_values[keys]` — verdict: keep
- [x] `test_to_json_value_rejects_unsafe_or_unsupported_values[pydantic-set]` — verdict: keep
- [x] `test_to_json_value_rejects_unsafe_or_unsupported_values[secret]` — verdict: keep
- [x] `test_to_json_value_rejects_unsafe_or_unsupported_values[serializer]` — verdict: keep
- [x] `test_to_json_value_rejects_unsafe_or_unsupported_values[unsupported]` — verdict: keep
- [x] `test_to_json_value_rejects_unsafe_or_unsupported_values[recursive]` — verdict: keep

### `packages/shared/tests/test_shell_protocol.py`

- [x] `test_shell_frame_codec_roundtrip` — verdict: keep
- [x] `test_shell_frame_codec_handles_split_and_partial_delivery` — verdict: keep
- [x] `test_shell_frame_codec_rejects_oversized_frames` — verdict: keep

### `packages/shared/tests/test_storage_contracts.py`

- [x] `test_queue_message_round_trip_preserves_json_body_and_claim_state` — verdict: keep
- [x] `test_secret_record_masks_values_and_excludes_them_from_representations` — verdict: keep
- [x] `test_object_write_command_rejects_mutation_and_negative_storage_counts` — verdict: **update** — The ObjectWriteCommand assertions are sound - frozen=True at packages/shared/src/shared/objects.py:14 makes the write intent immutable, and size=Field(ge=0) at objects.py:19 blocks a negative object size, both data-integrity invariants. The trailing CacheEntry block at packages/shared/tests/test_storage_contracts.py:73-82 does not belong: it feeds an unknown 'policy' key and expects rejection, but CacheEntry has no such field (packages/shared/src/shared/cache_records.py:11-19), so all it proves is that ContractModel sets extra='forbid' (packages/shared/src/shared/contracts.py:5). That is a base-class config default applied to every model in the package, not a behaviour of CacheEntry, and the misleading key name 'legacy-policy' implies a removed capability that this repository has no record of.
  - **How:** Delete the CacheEntry extra-field case at packages/shared/tests/test_storage_contracts.py:73-82. Keep the ObjectWriteCommand frozen/negative-size cases (lines 52-70) and the CacheEntry hits=-1 case (line 72), which exercise real field constraints.
- [x] `test_workspace_upload_contract_accepts_only_server_owned_bucket_purposes[default]` — verdict: keep
- [x] `test_workspace_upload_contract_accepts_only_server_owned_bucket_purposes[lazycloud-source-packages]` — verdict: keep
- [x] `test_workspace_upload_contract_accepts_only_server_owned_bucket_purposes[build-contexts]` — verdict: keep
- [x] `test_workspace_upload_contract_rejects_unsafe_metadata[metadata0]` — verdict: keep
- [x] `test_workspace_upload_contract_rejects_unsafe_metadata[metadata1]` — verdict: keep
- [x] `test_workspace_upload_contract_rejects_unsafe_metadata[metadata2]` — verdict: keep
- [x] `test_workspace_upload_contract_rejects_unsafe_metadata[metadata3]` — verdict: keep
- [x] `test_workspace_upload_contract_rejects_unsafe_content_type[text/plain\r\ninjected: true]` — verdict: keep
- [x] `test_workspace_upload_contract_rejects_unsafe_content_type[text/plain\x00raw]` — verdict: keep

### `packages/shared/tests/test_transport_retry.py`

- [x] `test_transient_transport_error_classification[error0-True]` — verdict: keep
- [x] `test_transient_transport_error_classification[error1-True]` — verdict: keep
- [x] `test_transient_transport_error_classification[error2-True]` — verdict: keep
- [x] `test_transient_transport_error_classification[error3-True]` — verdict: keep
- [x] `test_transient_transport_error_classification[error4-True]` — verdict: keep
- [x] `test_transient_transport_error_classification[error5-False]` — verdict: keep
- [x] `test_transient_transport_error_classification[error6-False]` — verdict: keep
- [x] `test_transient_transport_error_classification[error7-False]` — verdict: keep
- [x] `test_transient_transport_error_classification[error8-False]` — verdict: keep

### `packages/shared/tests/test_urls.py`

- [x] `test_normalize_http_origin_rejects_non_origins[]` — verdict: keep
- [x] `test_normalize_http_origin_rejects_non_origins[control.example.test]` — verdict: keep
- [x] `test_normalize_http_origin_rejects_non_origins[ftp://control.example.test]` — verdict: keep
- [x] `test_normalize_http_origin_rejects_non_origins[https://]` — verdict: keep
- [x] `test_normalize_http_origin_rejects_non_origins[https://user:secret@control.example.test]` — verdict: keep
- [x] `test_normalize_http_origin_rejects_non_origins[https://control.example.test/api]` — verdict: keep
- [x] `test_normalize_http_origin_rejects_non_origins[https://control.example.test?workspace=one]` — verdict: keep
- [x] `test_normalize_http_origin_rejects_non_origins[https://control.example.test#fragment]` — verdict: keep
- [x] `test_normalize_http_origin_rejects_non_origins[https://control.example.test:0]` — verdict: keep
- [x] `test_normalize_http_origin_rejects_non_origins[https://control.example.test:99999]` — verdict: keep

### `packages/shared/tests/test_usage_contracts.py`

- [x] `test_usage_contracts_preserve_recursive_json_metadata` — verdict: keep
- [x] `test_usage_contracts_reject_non_json_metadata` — verdict: keep
- [x] `test_usage_identity_is_deterministic` — verdict: **update** — Tautology: the whole test compares usage_record_id('task_count', 'workspace-1', 123) against itself (packages/shared/tests/test_usage_contracts.py:62-66). usage_record_id is uuid5 over a joined string (packages/shared/src/shared/usage.py:147-149), so self-equality holds by construction and the assertion can only fail if someone swapped in uuid4. The invariant that actually matters is billing idempotency: these ids are the dedup keys for metered usage across five producers (packages/scheduler/src/scheduler/containers.py:416, packages/worker/src/worker/supervision.py:355, packages/storage/src/storage/volume_metering.py:221, packages/gateway/src/gateway/service.py:2435, packages/compute/src/compute/service.py:4497). The half the test omits - that a different workspace, metric, or resource yields a different id - is the half whose failure would cross-credit or merge one tenant's usage into another's.
  - **How:** Assert the discriminating half as well: keep one same-inputs-same-id assertion, and add that usage_record_id('task_count', 'workspace-1', 123) differs from usage_record_id('task_count', 'workspace-2', 123) and from usage_record_id('cpu_seconds', 'workspace-1', 123) and from usage_record_id('task_count', 'workspace-1', 124), so a change to the part joining in packages/shared/src/shared/usage.py:148 that collapses distinct tenants or metrics into one id fails the test.

### `packages/shared/tests/test_workload_defaults.py`

- [x] `test_deployment_concurrency_is_positive_at_public_http_boundaries` — verdict: keep
- [x] `test_deployment_authoring_accepts_only_a_placement_target` — verdict: keep
- [x] `test_raw_deployment_persists_canonical_runtime_defaults[endpoint-1.0-128Mi-180-180-None]` — verdict: keep
- [x] `test_raw_deployment_persists_canonical_runtime_defaults[task-queue-1.0-128Mi-3600-10-3]` — verdict: keep

### `packages/storage-client/tests/test_s3_client_lifecycle.py`

- [x] `test_close_is_idempotent_and_closes_primary_and_presign_clients` — verdict: keep
- [x] `test_close_attempts_every_client_and_aggregates_failures_in_order` — verdict: keep
- [x] `test_close_closes_a_shared_primary_and_presign_client_once` — verdict: keep
- [x] `test_presigned_upload_binds_exact_headers_and_temporary_session_lifetime` — verdict: keep
- [x] `test_object_store_settings_repr_never_contains_credentials` — verdict: keep
- [x] `test_native_s3_presign_uses_explicit_session_token` — verdict: keep
- [x] `test_native_s3_presign_can_use_ambient_session_credentials` — verdict: keep

### `packages/storage/tests/test_cache_http_security.py`

- [x] `test_cache_http_exposes_only_authenticated_health_head_get_put_and_metadata` — verdict: keep
- [x] `test_cache_http_put_validates_hash_and_streams_standard_ranges` — verdict: keep
- [x] `test_cache_client_distinguishes_miss_short_read_corruption_and_outage` — verdict: keep
- [x] `test_cache_startup_removes_temporary_writes_and_reconciles_metadata` — verdict: keep
- [x] `test_cache_capacity_reservations_prevent_concurrent_overcommit` — verdict: keep
- [x] `test_cache_admission_honors_disk_pressure_and_metadata_bound` — verdict: keep
- [x] `test_cache_http_rejects_oversize_and_incomplete_uploads` — verdict: keep

### `packages/storage/tests/test_cache_mutation.py`

- [x] `test_mounted_cache_materializations_are_immutable` — verdict: keep
- [x] `test_cache_put_compensates_its_new_object_when_relational_write_fails` — verdict: keep
- [x] `test_cache_put_retires_the_previous_object_after_pointer_commit` — verdict: keep
- [x] `test_failed_old_object_retirement_is_reconciled_as_an_orphan` — verdict: keep
- [x] `test_cache_delete_leaves_row_as_retry_evidence_until_bytes_are_removed` — verdict: keep
- [x] `test_cache_row_delete_failure_is_retryable_after_bytes_are_gone` — verdict: keep
- [x] `test_cache_reconciliation_removes_missing_records_and_orphan_bytes` — verdict: keep
- [x] `test_cache_reconciliation_removes_abandoned_staging_file` — verdict: keep
- [x] `test_cache_reconciliation_scans_past_valid_first_candidates` — verdict: keep
- [x] `test_concurrent_cache_puts_leave_one_matching_pointer_and_object` — verdict: keep

### `packages/storage/tests/test_collection_services.py`

- [x] `test_redis_map_service_sets_indexes_lists_live_keys_and_removes_stale_entries` — verdict: **update** — Wrong owner plus duplicated coverage. The file lives under packages/storage/tests but exercises `execution.collections.redis.RedisMapService` (packages/storage/tests/test_collection_services.py:5-6, :17), which packages/execution/CLAUDE.md explicitly owns ('Redis collections/signals'); packages/storage/CLAUDE.md scopes storage to object/cache storage and mounted filesystems. The majority of its assertions — map_set/map_get, per-key TTL, map_keys ordering, and map_stats count/size_bytes/expiring_keys/nearest_expiry — are already proven against the same real Redis at the cheaper authoritative owner, packages/execution/tests/test_redis_collections.py:13-27, which additionally proves cross-workspace preservation on delete_workspace (:29-32). Only the stale-index reconciliation (packages/storage/tests/test_collection_services.py:34-36), the NotFoundError on a missing key (:38-39), and the MAX_MAP_VALUE_SIZE_BYTES / MAX_MAP_TTL_SECONDS limits (:43-52) are unique and worth keeping.
  - **How:** Delete packages/storage/tests/test_collection_services.py and fold its three unique invariants into the execution owner suite: add to packages/execution/tests/test_redis_collections.py a case that (a) deletes the value key directly and asserts map_keys/map_count and the `map:...:index` set drop the stale entry and map_get raises NotFoundError, and (b) asserts map_set rejects a value over MAX_MAP_VALUE_SIZE_BYTES and a ttl over MAX_MAP_TTL_SECONDS. Drop the round-trip/stats assertions that duplicate packages/execution/tests/test_redis_collections.py:19-27.

### `packages/storage/tests/test_image_archive_settings.py`

- [x] `test_image_archive_rejects_ambiguous_bucket_and_backend` — verdict: keep

### `packages/storage/tests/test_retention_settings.py`

- [x] `test_retention_settings_reject_inverted_retry_window` — verdict: **delete** — Fails gate criterion 2 (no material consequence). The whole test is one Pydantic model_validator on a settings object (packages/storage/src/storage/retention_settings.py:66-72). The only consumer of those two fields is the retention backoff at packages/scheduler/src/scheduler/service.py:626-631, `retry_seconds = min(retry_max, retry_initial * 2**(n-1))`; an inverted window merely makes the first retry fire sooner than `retry_initial`. Nothing about authorization, security, data integrity, durability, concurrency, cleanup, a public contract, or a user-visible terminal outcome depends on it. It is the config-guard-rail / third-party-defaults shape the repository's 'Do not test' list rules out, and it is not the cheapest proof of any production decision — it is the only assertion about a value that never leaves the scheduler process.

### `packages/storage/tests/test_volume_metering.py`

- [x] `test_volume_metering_records_byte_seconds_and_advances_checkpoint` — verdict: keep
- [x] `test_volume_metering_scans_only_the_stable_volume_namespace` — verdict: keep
- [x] `test_final_volume_metering_closes_checkpoint_window_when_scan_fails` — verdict: keep
- [x] `test_scheduler_meters_volumes_even_when_workload_loops_are_disabled` — verdict: **update** — States a real invariant as a mock transcript at the wrong owner. The invariant — volume metering is not gated by include_cron_jobs/include_containers, so billing keeps accruing when the workload loops are off (packages/scheduler/src/scheduler/service.py:498-501 calls `_meter_persistent_volumes` unconditionally, unlike every sibling at :502-557) — is material for revenue/usage integrity and is worth keeping. But the test injects `_RecordingMeter` (packages/storage/tests/test_volume_metering.py:28-39) and its load-bearing assertion is `meter.calls == [(now, 17)]` (:196), a fake's call log; tests/CLAUDE.md and the repository 'Do not test' list forbid asserting calls/mock transcripts rather than resulting state. It also drives `scheduler.service.Scheduler` from packages/storage/tests, while packages/scheduler/CLAUDE.md owns the scheduler.
  - **How:** Move the case to the scheduler owner suite and assert durable outcome instead of the fake's call log: build the pool/volume as packages/storage/tests/test_volume_metering.py:42-98 does, wire a real `PersistentVolumeMeteringService` into `SchedulerMaintenanceControls`, call `Scheduler.run_once(now=..., include_cron_jobs=False, include_containers=False)`, then assert a `UsageMetric.PersistentVolumeByteSeconds` record was written and `VolumeTable.metered_at` advanced to `now`. Drop the `_RecordingMeter` fake and the `meter.calls` assertion; keep `volume_metering_count`/`volume_metering_failure_count` only if they are read from the real service result.

### `packages/storage/tests/test_volume_object_storage.py`

- [x] `test_volume_control_isolates_same_name_by_stable_workspace_and_volume_ids` — verdict: keep
- [x] `test_workspace_volumes_sharing_a_volume_id_stay_in_their_own_buckets` — verdict: keep
- [x] `test_move_path_rejects_an_occupied_destination_without_touching_either_side` — verdict: keep
- [x] `test_volume_control_presigned_and_multipart_requests_use_database_identity` — verdict: keep
- [x] `test_delete_path_does_not_delete_sibling_prefixes` — verdict: keep
- [x] `test_storage_delete_failure_keeps_volume_metadata_retriable` — verdict: keep

### `packages/storage/tests/test_workspace_storage.py`

- [x] `test_workspace_create_sets_up_default_storage_and_primary_token` — verdict: keep
- [x] `test_workspace_storage_creation_validates_before_persisting` — verdict: keep
- [x] `test_external_workspace_storage_validates_rejects_duplicates_and_checks_scope` — verdict: keep
- [x] `test_workspace_storage_api_keeps_token_active_after_cache_invalidation_hook` — verdict: keep
- [x] `test_workspace_objects_with_same_logical_location_are_physically_isolated` — verdict: keep
- [x] `test_logical_object_purposes_share_one_physical_bucket_with_distinct_prefixes` — verdict: keep
- [x] `test_separate_allowed_backend_keeps_its_physical_bucket` — verdict: **delete** — Tautological pass-through with no state, side effect, or outcome. The test (packages/storage/tests/test_workspace_storage.py:362-372) constructs `ObjectStorage(..., allowed_buckets=("separate-archive-bucket",))` and asserts `physical_bucket("separate-archive-bucket") == "separate-archive-bucket"` — it asserts the service against its own constructor input, exercising only the `return bucket` fall-through at packages/storage/src/storage/service.py:1023. No object is written, no record persisted, no authorization or tenant boundary crossed, so failure cannot affect data integrity, durability, or cleanup (criterion 2). The bucket-mapping decision that actually matters — logical purposes collapsing into one physical bucket under distinct per-workspace prefixes — is already proven with real objects and real records at packages/storage/tests/test_workspace_storage.py:321-359, and the allowed-bucket rejection path is enforced by `_validate_bucket` at packages/storage/src/storage/service.py:1025-1026 on every real put/read exercised by packages/storage/tests/test_workspace_storage.py:269-318.
- [x] `test_immutable_file_replay_reuses_complete_object_and_repairs_missing_bytes` — verdict: keep
- [x] `test_object_completeness_requires_exact_metadata_and_maps_store_outages` — verdict: keep

### `packages/worker-repository/tests/test_source_cache_service.py`

- [x] `test_private_worker_cannot_resolve_another_workspace_cache_claim` — verdict: keep

### `packages/worker-repository/tests/test_source_cache_status.py`

- [x] `test_source_cache_cleanup_status_is_bounded_and_resolves_deleted_workspace` — verdict: keep
- [x] `test_source_cache_cleanup_status_clamps_future_clock_and_reports_missing` — verdict: keep

### `packages/worker-repository/tests/test_worker_cache_origin_credentials.py`

- [x] `test_cache_origin_credentials_vend_only_archive_url` — verdict: keep
- [x] `test_image_archive_vending_requires_injected_lifespan_signer` — verdict: keep
- [x] `test_image_archive_presign_failures_are_sanitized` — verdict: keep
- [x] `test_cache_origin_credentials_restrict_private_worker_workspace` — verdict: keep

### `packages/worker-repository/tests/test_worker_checkpoint_creation_leases.py`

- [x] `test_automatic_checkpoint_creation_lease_serializes_first_creator` — verdict: keep
- [x] `test_automatic_checkpoint_creation_lease_rechecks_available_artifact_after_lock` — verdict: keep

### `packages/worker-repository/tests/test_worker_credentials_service.py`

- [x] `test_worker_credential_service_vends_requested_bundle` — verdict: keep
- [x] `test_worker_credential_service_reuses_gateway_token_across_containers` — verdict: keep
- [x] `test_worker_credential_service_replaces_revoked_or_aging_gateway_tokens` — verdict: **update** — The credential-rotation invariant (a revoked lease and a lease in the back half of its TTL are both replaced rather than handed to a container) is worth keeping, but the aging half is stated through implementation shape: packages/worker-repository/tests/test_worker_credentials_service.py:201-204 reaches into the private, init=False field `service._gateway_token_leases` (packages/worker-repository/src/worker_repository/credentials.py:130-132) and mutates the private `_GatewayTokenLease.expires_at` dataclass (credentials.py:115-118). Nothing about that reach-in is production behaviour — the production decision is `remaining_seconds < self.gateway_token_ttl_seconds / 2` at credentials.py:252 — and the test breaks on any rename of a private member while a real regression in the rotation rule could still pass. The revoke half (test file:190-198) already drives production through the public AuthService and needs no change.
  - **How:** Replace the private-attribute mutation at test_worker_credentials_service.py:201-204 with a change to the public dataclass field `service.gateway_token_ttl_seconds` (credentials.py:127) — raise it so the already-stored lease's remaining lifetime falls below half of the new TTL — then vend again and assert the token changed. Same invariant, driven entirely through the service's public surface.
- [x] `test_worker_credential_service_resolves_volume_secret_names` — verdict: keep
- [x] `test_worker_credential_service_rejects_invalid_principal_and_assignment` — verdict: keep
- [x] `test_worker_credential_service_rejects_unavailable_secret_storage_and_mount` — verdict: keep
- [x] `test_worker_credential_hydrator_applies_credentials_to_execution_context` — verdict: keep

### `packages/worker/tests/test_container_service_client.py`

- [x] `test_container_log_stream_filters_empty_entries_and_emits_keepalives` — verdict: **delete** — The keepalive it asserts has no production effect and the filtering it asserts is already done by the consumer. stream_logs' keepalive thread emits OutputMessage(msg="") to the local callback only (packages/worker/src/worker/container_client/control.py:551-556) - it touches no socket, so it cannot hold a stream open. The sole production consumer, ImageBuildLogStreamCollector._capture, drops every blank message at packages/images/src/images/log_streaming.py:129, so both the empty-entry filter and the keepalive are unobservable. What remains is a mock transcript (line 82, transport.stream_calls[0].method) and a sleep-race assertion (line 65 sleeps 0.02s against a 0.005s interval at line 78).
- [x] `test_container_archive_outputs_progress_errors_and_success` — verdict: **delete** — Asserts literal terminal presentation copy and a mock transcript, both on the "Do not test" list. packages/worker/tests/test_container_service_client.py:105-110 pins the exact ANSI progress-bar string "\033[A\r[============      ] 25%\n" and the "\nSaving image" banner produced by generate_progress_bar (packages/worker/src/worker/container_client/control.py:520-533), and line 102 asserts transport.stream_calls[0].method. No material outcome is covered: the only terminal decision in archive() is raising ContainerArchiveError when done and not success (packages/worker/src/worker/container_client/control.py:456-458), and this test never exercises that branch.

### `packages/worker/tests/test_runner_managed_runtime_launcher.py`

- [x] `test_launcher_rejects_incompatible_user_dependency` — verdict: keep
- [x] `test_launcher_validates_selected_user_dependency_graph` — verdict: keep
- [x] `test_launcher_uses_locked_verifier_without_changing_user_precedence` — verdict: keep
- [x] `test_launcher_places_managed_packages_before_user_package_shadows` — verdict: keep

### `packages/worker/tests/test_worker_adapters.py`

- [x] `test_worker_finalization_rejects_a_bundle_path_owned_by_another_container` — verdict: keep
- [x] `test_worker_controller_finalization_cleanup_force_stops_live_runtime_states` — verdict: keep
- [x] `test_worker_runtime_container_stopper_escalates_ignored_graceful_signal` — verdict: keep
- [x] `test_worker_runtime_container_stopper_does_not_force_exited_container` — verdict: keep
- [x] `test_worker_runtime_container_stopper_rejects_missing_durable_assignment` — verdict: keep
- [x] `test_worker_runtime_container_stopper_rejects_foreign_assignment` — verdict: keep

### `packages/worker/tests/test_worker_automatic_checkpoints.py`

- [x] `test_automatic_checkpoint_mounts_signal_and_publishes_after_runner_ready` — verdict: keep
- [x] `test_checkpoint_restore_completes_runner_identity_handshake` — verdict: keep
- [x] `test_automatic_checkpoint_lease_denial_releases_runner_without_duplicate_creation` — verdict: keep
- [x] `test_automatic_checkpoint_stops_waiting_when_runtime_exits` — verdict: keep

### `packages/worker/tests/test_worker_cache_storage_registry.py`

- [x] `test_worker_lifecycle_orchestrates_keepalive_shutdown_usage_and_cleanup` — verdict: keep

### `packages/worker/tests/test_worker_checkpoint_lifecycle.py`

- [x] `test_checkpoint_archive_materialization_rejects_incomplete_and_unavailable_sources` — verdict: keep

### `packages/worker/tests/test_worker_checkpoint_restore.py`

- [x] `test_runtime_checkpoint_restorer_reuses_owned_rootfs_after_source_image_eviction` — verdict: keep
- [x] `test_runtime_checkpoint_restorer_rejects_corrupt_archive_and_marks_failure` — verdict: keep
- [x] `test_runtime_checkpoint_restorer_keeps_checkpoint_available_after_started_exit` — verdict: keep
- [x] `test_runtime_checkpoint_restorer_preserves_fresh_rootfs_for_deployment_fallback` — verdict: keep
- [x] `test_concurrent_restores_materialize_once_then_run_independently` — verdict: keep
- [x] `test_failed_materialization_releases_owner_for_waiting_restore_retry` — verdict: keep
- [x] `test_materialization_ownership_is_per_checkpoint_and_retention_safe` — verdict: keep

### `packages/worker/tests/test_worker_configuration.py`

- [x] `test_worker_configuration_serializes_as_nested_yaml_without_credentials` — verdict: **update** — The YAML round-trip is worth proving - the agent writes this file at apps/agent/src/agent_app/daemon.py:1769 and the worker process reads it back through YamlConfigSettingsSource under the same section key at apps/container-worker/src/container_worker_app/production.py:236-241 - but three of the four assertions are vacuous. WorkerConfiguration has no credential field at all (packages/worker/src/worker/configuration.py:132-143 holds only execution/network/paths/monitoring/source_cache/image_build), so "token" not in contents (line 37) and "secret" not in contents (line 38) cannot fail for any current production behavior, and line 36's cpu_millicores == 2500 is already subsumed by effective == config on line 35.
  - **How:** Keep the serialize -> yaml.safe_load -> validate under WORKER_CONFIGURATION_SECTION -> effective == config round-trip and drop lines 36-38. Rename the test to what it proves (the agent-written worker.yaml section parses back into an identical WorkerConfiguration). If secret redaction is genuinely wanted, it belongs wherever a credential-bearing settings model is actually serialized, not on a model that has no secret fields.

### `packages/worker/tests/test_worker_container_checkpoints.py`

- [x] `test_runtime_checkpoint_creator_runs_runtime_persists_archive_and_records_state` — verdict: keep
- [x] `test_runtime_checkpoint_creator_records_failed_state_on_runtime_error` — verdict: keep
- [x] `test_container_filesystem_archive_creator_rejects_non_running_container` — verdict: keep
- [x] `test_container_filesystem_archive_creator_requires_durable_publication` — verdict: keep

### `packages/worker/tests/test_worker_container_execution.py`

- [x] `test_worker_container_execution_service_runs_full_lifecycle` — verdict: keep
- [x] `test_worker_container_execution_fails_before_running_when_docker_startup_fails` — verdict: keep
- [x] `test_checkpoint_startup_is_monitored_before_running_and_route_publication` — verdict: keep
- [x] `test_deployment_restore_fallback_starts_fresh_and_completes_handshake` — verdict: keep
- [x] `test_restore_fallback_and_fresh_run_share_one_log_capture_and_flush` — verdict: keep
- [x] `test_worker_container_execution_cleans_runtime_when_cancelled_after_start` — verdict: keep
- [x] `test_worker_container_execution_service_handles_image_short_circuit` — verdict: keep
- [x] `test_worker_container_execution_service_handles_oom_before_finalization` — verdict: keep
- [x] `test_worker_container_execution_service_assigns_gpus_before_building_spec` — verdict: keep
- [x] `test_worker_container_execution_service_stops_on_gpu_assignment_failure` — verdict: keep
- [x] `test_worker_container_execution_redacts_runtime_start_failure_output` — verdict: keep
- [x] `test_worker_container_execution_service_keeps_running_when_lifecycle_sink_fails` — verdict: keep
- [x] `test_worker_container_execution_service_stops_on_mount_failure` — verdict: keep

### `packages/worker/tests/test_worker_container_log_repository_client.py`

- [x] `test_container_log_batch_rejects_sequence_gaps_and_invalid_kind_fields` — verdict: keep

### `packages/worker/tests/test_worker_container_logs.py`

- [x] `test_container_log_capture_frames_partial_lines_and_preserves_streams` — verdict: keep
- [x] `test_container_log_capture_splits_utf8_without_breaking_code_points` — verdict: keep
- [x] `test_container_log_capture_close_materializes_drop_and_flush_when_queue_full` — verdict: keep
- [x] `test_container_log_capture_retries_the_same_idempotent_batch` — verdict: keep
- [x] `test_container_log_capture_rate_limit_is_reported_without_sequence_gap` — verdict: keep
- [x] `test_container_log_capture_outage_stops_at_flush_deadline` — verdict: keep
- [x] `test_container_log_capture_rejects_ack_beyond_submitted_batch` — verdict: keep
- [x] `test_container_log_capture_can_resume_an_explicit_capture_sequence` — verdict: keep

### `packages/worker/tests/test_worker_container_metrics.py`

- [x] `test_worker_container_metrics_service_computes_deltas_and_publishes` — verdict: keep
- [x] `test_worker_container_runtime_monitor_publishes_metrics_and_usage_on_stop` — verdict: keep
- [x] `test_worker_container_metrics_service_primes_without_publishing_first_sample` — verdict: keep

### `packages/worker/tests/test_worker_container_service.py`

- [x] `test_worker_container_service_exec_persists_sandbox_process_logs` — verdict: keep
- [x] `test_worker_container_service_runtime_and_process_operations` — verdict: keep
- [x] `test_supervisor_transport_authenticates_without_exposing_token_in_repr` — verdict: keep
- [x] `test_worker_container_service_kill_treats_missing_runtime_container_as_stopped` — verdict: keep
- [x] `test_worker_container_service_file_operations_and_workspace_sync` — verdict: keep
- [x] `test_worker_container_service_sandboxed_download_streams_from_supervisor` — verdict: keep
- [x] `test_worker_container_service_exposes_ports_and_updates_network` — verdict: keep
- [x] `test_restored_worker_instance_lists_persisted_exposed_ports` — verdict: keep
- [x] `test_worker_container_service_unexposes_only_requested_port` — verdict: keep
- [x] `test_worker_container_service_network_update_fails_without_policy_updater` — verdict: keep
- [x] `test_worker_container_service_network_update_does_not_persist_after_policy_error` — verdict: keep

### `packages/worker/tests/test_worker_event_bridge.py`

- [x] `test_worker_stream_event_handler_stops_containers_and_cancels_builds` — verdict: keep
- [x] `test_worker_stream_event_handler_reports_missing_stopper` — verdict: **delete** — Tests a defensive default that production never reaches, and asserts its error-message literal. packages/worker/tests/test_worker_event_bridge.py:58-70 constructs WorkerStreamEventHandler() with no arguments to hit the container_stopper is None branch at packages/worker/src/worker/event_bridge.py:138-148. The only production construction always passes a non-optional stopper: runtime_stopper is built unconditionally at apps/container-worker/src/container_worker_app/process_assembly.py:215 and passed at line 344. This is constructor-wiring/default-value coverage, not production behavior.
- [x] `test_worker_stream_event_handler_does_not_acknowledge_failed_container_stop` — verdict: keep

### `packages/worker/tests/test_worker_finalization.py`

- [x] `test_worker_container_finalizer_records_exit_and_immediate_cleanup` — verdict: keep
- [x] `test_worker_container_finalizer_delayed_cleanup_forces_and_deletes_state` — verdict: keep
- [x] `test_worker_container_finalizer_uses_stop_reason_exit_code_and_skips_gpu_release` — verdict: keep
- [x] `test_worker_container_finalizer_records_preemption_as_distinct_exit` — verdict: keep
- [x] `test_worker_container_finalizer_captures_cleanup_errors_and_continues` — verdict: keep

### `packages/worker/tests/test_worker_gpu_planning.py`

- [x] `test_gpu_allocation_manager_assigns_unassigns_and_denies_exhaustion` — verdict: keep
- [x] `test_dynamic_gpu_allocation_manager_uses_provider_and_releases` — verdict: keep

### `packages/worker/tests/test_worker_image_archive_transfer.py`

- [x] `test_image_archive_transfers_retry_transient_responses_and_preserve_signed_headers` — verdict: keep
- [x] `test_image_archive_download_integrity_failure_is_terminal_and_preserves_target` — verdict: keep
- [x] `test_image_archive_transfer_error_never_discloses_capability_query` — verdict: keep
- [x] `test_image_archive_unexpected_client_error_never_discloses_capability_query` — verdict: keep

### `packages/worker/tests/test_worker_image_build_architecture.py`

- [x] `test_buildah_targets_requested_architecture_for_build_and_from_operations[build_options0]` — verdict: **delete** — Asserts a generated command and call order through a monkeypatched private method, all explicitly on the "Do not test" list. packages/worker/tests/test_worker_image_build_architecture.py:85 replaces BuildahWorkerImageBuilder._run_buildah, then line 131 asserts command[1:3] == ["--arch", "amd64"] and lines 127-128 assert the preparer/buildah call ordering. No resulting state, archive, or error is checked - the archiver is a stub that writes b"archive" (line 43). Real evidence that a requested architecture is honored is the produced image from a cross-architecture build through a real worker/container, which packages/worker/AGENTS.md requires when OCI/build behavior changes.
- [x] `test_buildah_targets_requested_architecture_for_build_and_from_operations[build_options1]` — verdict: **delete** — Same generated-command assertion as build_options0 with source_image instead of dockerfile; both rows land on the identical argv check at packages/worker/tests/test_worker_image_build_architecture.py:131 through the monkeypatched private _run_buildah at line 85. The row adds no distinct high-risk transition - only whether the argv verb is "from" instead of "bud", which line 130 already collapses into one set.
- [x] `test_native_image_build_architecture_requires_no_binfmt_support` — verdict: keep
- [x] `test_cross_architecture_registers_bundled_persistent_handler[arm64-amd64-x86_64]` — verdict: keep
- [x] `test_cross_architecture_registers_bundled_persistent_handler[x86_64-arm64-aarch64]` — verdict: keep
- [x] `test_cross_architecture_reuses_existing_persistent_handler` — verdict: keep
- [x] `test_cross_architecture_reports_missing_worker_privilege` — verdict: keep

### `packages/worker/tests/test_worker_image_build_execution.py`

- [x] `test_remote_image_build_credential_loader_consumes_once_without_polling` — verdict: **delete** — Tautology asserting a pass-through against its own input. RemoteImageBuildCredentialLoader.load (packages/worker/src/worker/image_build_runtime_credentials.py:35-51) makes no decision: it packs its five keyword arguments into GetImageBuildCredentialsRequest, calls the repository once, and returns private_inputs or an empty value. packages/worker/tests/test_worker_image_build_execution.py:77-84 asserts the recorded request equals those same five arguments, which is a mock transcript of a wrapper. The material invariant - the build worker binds the credential fetch to the authenticated workspace and the request's cache key rather than the client-supplied one - is proven through the real service at packages/worker/tests/test_worker_image_build_execution.py:178-186.
- [x] `test_image_build_worker_rejects_managed_package_version_mismatch` — verdict: keep
- [x] `test_image_build_worker_consumes_bound_source_credentials` — verdict: keep
- [x] `test_worker_registry_authfile_is_private_and_contains_docker_auth` — verdict: keep
- [x] `test_worker_private_build_args_are_redacted_from_results_and_instance_logs` — verdict: keep
- [x] `test_worker_publication_failure_redacts_private_logs_and_error` — verdict: keep
- [x] `test_repository_archive_publisher_requests_and_propagates_exact_identity` — verdict: keep
- [x] `test_buildah_failure_cleans_every_resource_in_its_isolated_store` — verdict: keep
- [x] `test_buildah_cleanup_failure_still_releases_isolated_scratch` — verdict: keep
- [x] `test_repository_build_context_loader_rejects_incomplete_or_modified_body[1-None-body is incomplete]` — verdict: keep
- [x] `test_repository_build_context_loader_rejects_incomplete_or_modified_body[0-0000000000000000000000000000000000000000000000000000000000000000-SHA-256 does not match]` — verdict: keep
- [x] `test_repository_build_context_error_never_discloses_capability_query` — verdict: keep

### `packages/worker/tests/test_worker_image_build_scratch.py`

- [x] `test_scratch_admission_reserves_bounded_isolated_build_roots` — verdict: keep
- [x] `test_reconcile_skips_locked_build_then_removes_interrupted_build` — verdict: keep
- [x] `test_release_closes_lease_when_build_root_disappeared` — verdict: keep

### `packages/worker/tests/test_worker_lifecycle.py`

- [x] `test_platform_volume_resolves_into_workspace_storage` — verdict: keep
- [x] `test_platform_volume_keeps_its_logical_path_without_workspace_storage` — verdict: keep
- [x] `test_volume_source_directory_is_created_inside_workspace_storage` — verdict: **update** — The docstring's invariant (the volume's directory must actually be created on the mounted filesystem, because the object-storage prefix is lazy) is material after the GeeseFS workspace-mount change, but the test does not prove it. packages/worker/tests/test_worker_lifecycle.py:65 asserts only that plan_bind_mount_source_dirs returns BindMountSourceDirAction.Create, which is the unconditional fall-through branch of a pure function (packages/worker/src/worker/lifecycle.py:769-776) reached for any non-MountPoint mount with a non-empty local_path supplied by the test itself. Nothing is created, so a regression in the code that does the mkdir would not fail this test.
  - **How:** Exercise ensure_bind_mount_source_dirs (packages/worker/src/worker/lifecycle.py:780-785) against a tmp_path-rooted RequestMount and assert the source directory exists on disk afterwards, instead of comparing the plan enum. Keep one MountPoint case only if it is asserted as a real side effect (no directory created).

### `packages/worker/tests/test_worker_managed_runtime.py`

- [x] `test_managed_runtime_wraps_target_interpreter_and_preserves_user_paths[function-runner.function]` — verdict: keep
- [x] `test_managed_runtime_wraps_target_interpreter_and_preserves_user_paths[endpoint-runner.serve]` — verdict: keep
- [x] `test_managed_runtime_wraps_target_interpreter_and_preserves_user_paths[asgi-runner.serve]` — verdict: keep
- [x] `test_managed_runtime_wraps_target_interpreter_and_preserves_user_paths[taskqueue-runner.taskqueue]` — verdict: keep

### `packages/worker/tests/test_worker_oci_runtime.py`

- [x] `test_oci_runtime_selects_persisted_container_runtime_after_process_restart` — verdict: keep
- [x] `test_oci_runtime_treats_runsc_missing_state_as_stopped` — verdict: keep
- [x] `test_oci_runtime_aborts_inflight_run_when_started_callback_rejects` — verdict: keep
- [x] `test_oci_runtime_bounds_hung_runsc_delete_during_start_abort` — verdict: keep

### `packages/worker/tests/test_worker_repository_client.py`

- [x] `test_worker_repository_client_preserves_session_auth_and_scoped_credentials` — verdict: keep

### `packages/worker/tests/test_worker_request_mounts.py`

- [x] `test_request_mount_manager_rejects_paths_outside_owned_root` — verdict: keep

### `packages/worker/tests/test_worker_retention.py`

- [x] `test_retention_preserves_active_local_clip_archive` — verdict: keep
- [x] `test_retention_preserves_checkpoint_with_active_checkpoint_lease` — verdict: keep
- [x] `test_retention_guard_serializes_new_checkpoint_lease` — verdict: keep
- [x] `test_retention_reclaims_interrupted_image_build_scratch_when_cache_pruning_disabled` — verdict: keep

### `packages/worker/tests/test_worker_sandbox_docker.py`

- [x] `test_docker_enabled_sandbox_starts_probes_and_stops_daemon` — verdict: keep
- [x] `test_concurrent_readiness_serializes_one_daemon_start` — verdict: keep
- [x] `test_docker_daemon_stream_disconnect_is_quiet_during_expected_stop` — verdict: keep
- [x] `test_unexpected_docker_daemon_stream_disconnect_records_failure` — verdict: keep
- [x] `test_unexpected_docker_daemon_exit_records_bounded_utf8_output` — verdict: keep

### `packages/worker/tests/test_worker_scheduler_requests.py`

- [x] `test_worker_scheduler_request_processor_executes_and_releases_capacity` — verdict: keep
- [x] `test_worker_scheduler_request_processor_executes_image_build_branch` — verdict: keep
- [x] `test_worker_scheduler_request_processor_tracks_active_container_for_shutdown` — verdict: keep
- [x] `test_worker_scheduler_request_processor_backgrounds_long_lived_container` — verdict: keep
- [x] `test_worker_scheduler_request_processor_drops_missing_state_and_releases_capacity` — verdict: keep
- [x] `test_worker_scheduler_request_processor_drops_stopping_state_and_deletes_state` — verdict: keep
- [x] `test_worker_scheduler_request_processor_reports_execution_failure` — verdict: keep

### `packages/worker/tests/test_worker_source_cache_cleanup.py`

- [x] `test_source_cache_marker_is_reused_and_replacement_gets_new_identity` — verdict: keep
- [x] `test_source_cache_marker_concurrent_open_installs_one_identity` — verdict: keep
- [x] `test_machine_cache_destruction_requires_bound_session_and_removes_root` — verdict: keep
- [x] `test_source_cache_identity_fails_closed_for_unsafe_marker_and_owner_matrix[session-binding]` — verdict: keep
- [x] `test_source_cache_identity_fails_closed_for_unsafe_marker_and_owner_matrix[storage-empty]` — verdict: keep
- [x] `test_source_cache_identity_fails_closed_for_unsafe_marker_and_owner_matrix[storage-name]` — verdict: keep
- [x] `test_source_cache_identity_fails_closed_for_unsafe_marker_and_owner_matrix[storage-pod]` — verdict: keep
- [x] `test_source_cache_identity_fails_closed_for_unsafe_marker_and_owner_matrix[marker-empty]` — verdict: keep
- [x] `test_source_cache_identity_fails_closed_for_unsafe_marker_and_owner_matrix[marker-malformed]` — verdict: keep
- [x] `test_source_cache_identity_fails_closed_for_unsafe_marker_and_owner_matrix[marker-symlink]` — verdict: keep
- [x] `test_source_cache_identity_fails_closed_for_unsafe_marker_and_owner_matrix[marker-directory]` — verdict: keep
- [x] `test_source_cache_identity_fails_closed_for_unsafe_marker_and_owner_matrix[missing-identity]` — verdict: keep
- [x] `test_source_cache_reconciliation_purges_exact_target_and_preserves_sibling` — verdict: keep

### `packages/worker/tests/test_worker_source_code.py`

- [x] `test_source_materializer_cleans_only_the_terminal_container_paths` — verdict: keep
- [x] `test_source_materializer_prunes_only_inactive_owned_temporary_paths` — verdict: keep
- [x] `test_source_materializer_enforces_entry_and_byte_caps_after_copy` — verdict: keep
- [x] `test_source_materializer_rejects_container_path_traversal` — verdict: keep
- [x] `test_source_download_error_never_discloses_capability_query` — verdict: keep

### `packages/worker/tests/test_worker_supervision.py`

- [x] `test_worker_supervision_handles_sandbox_oom_with_forced_stop` — verdict: keep
- [x] `test_worker_supervision_records_runc_oom_without_stop` — verdict: keep
- [x] `test_worker_supervision_records_usage_records` — verdict: keep
- [x] `test_worker_supervision_usage_windows_are_idempotent` — verdict: keep
- [x] `test_worker_supervision_records_private_pool_usage_for_managed_billing_owner` — verdict: keep
- [x] `test_worker_supervision_requires_ordered_timezone_aware_metering_window` — verdict: keep
- [x] `test_container_duration_usage_requires_complete_allocation_evidence[seconds-labels0]` — verdict: keep
- [x] `test_container_duration_usage_requires_complete_allocation_evidence[milliseconds-labels1]` — verdict: keep
- [x] `test_container_duration_usage_requires_complete_allocation_evidence[milliseconds-labels2]` — verdict: keep
- [x] `test_container_duration_usage_requires_complete_allocation_evidence[milliseconds-labels3]` — verdict: keep
- [x] `test_container_duration_usage_requires_complete_allocation_evidence[milliseconds-labels4]` — verdict: keep

### `packages/worker/tests/test_worker_tools.py`

- [x] `test_ambient_bucket_mount_does_not_request_credentials` — verdict: keep
- [x] `test_secret_reference_bucket_mount_requests_credentials_once` — verdict: keep

### `tests/deployment/test_agent_binary_build.py`

- [x] `test_stage_agent_binaries_writes_versioned_immutable_layout` — verdict: keep
- [x] `test_stage_agent_binaries_rejects_mutating_a_published_version` — verdict: keep
- [x] `test_stage_agent_binaries_rejects_non_linux_payload` — verdict: keep

### `tests/deployment/test_aws_release_assets.py`

- [x] `test_stage_release_binds_exact_immutable_runtime_assets` — verdict: keep
- [x] `test_stage_release_records_baked_cpu_ami_catalog` — verdict: keep
- [x] `test_local_validation_rejects_tampered_release_object` — verdict: keep
- [x] `test_stage_release_rejects_non_elf_agent` — verdict: keep
- [x] `test_stage_release_rejects_mutating_an_existing_version` — verdict: keep
- [x] `test_load_release_rejects_a_manifest_from_obsolete_source` — verdict: keep
- [x] `test_worker_image_verification_requires_exact_linux_amd64_digest` — verdict: keep
- [x] `test_worker_image_verification_accepts_buildkit_attested_amd64_index` — verdict: keep
- [x] `test_worker_image_verification_rejects_ambiguous_index[extra_descriptor0-exactly one runnable Linux amd64]` — verdict: keep
- [x] `test_worker_image_verification_rejects_ambiguous_index[extra_descriptor1-exactly one runnable Linux amd64]` — verdict: keep
- [x] `test_worker_image_verification_rejects_ambiguous_index[extra_descriptor2-attestation for another image]` — verdict: keep

### `tests/deployment/test_customer_compute_configuration.py`

- [x] `test_compose_keeps_gateway_identity_credential_in_sidecar_only` — verdict: keep

### `tests/deployment/test_helm_lazycloud_chart.py`

- [x] `test_helm_chart_rejects_worker_name_that_cannot_preserve_pool_identity` — verdict: keep
- [x] `test_helm_chart_rejects_inverted_cache_disk_watermarks` — verdict: keep
- [x] `test_helm_chart_rejects_non_origin_gateway_urls[controlPlane.publicHttpUrl-ftp://control.example.test]` — verdict: keep
- [x] `test_helm_chart_rejects_non_origin_gateway_urls[controlPlane.publicHttpUrl-https://user:secret@control.example.test]` — verdict: keep
- [x] `test_helm_chart_rejects_non_origin_gateway_urls[controlPlane.publicHttpUrl-https://control.example.test/api]` — verdict: keep
- [x] `test_helm_chart_rejects_non_origin_gateway_urls[controlPlane.publicHttpUrl-https://control.example.test?query=one]` — verdict: keep
- [x] `test_helm_chart_rejects_non_origin_gateway_urls[controlPlane.publicHttpUrl-https://control.example.test#fragment]` — verdict: keep
- [x] `test_helm_chart_rejects_non_origin_gateway_urls[controlPlane.publicHttpUrl-https://]` — verdict: keep
- [x] `test_helm_chart_rejects_non_origin_gateway_urls[controlPlane.publicHttpUrl-https://control.example.test:99999]` — verdict: keep
- [x] `test_helm_chart_rejects_non_origin_gateway_urls[controlPlane.runtimeHttpUrl-http://release-lazycloud:9000/callback]` — verdict: keep
- [x] `test_helm_local_render_contains_required_services_and_secure_pods` — verdict: keep
- [x] `test_helm_aws_capacity_uses_provider_identity_and_secret_refs` — verdict: **update** — Currently fails: `helm template` exits 1 with 'awsCapacity.agentArtifactUrl is required'. The chart requires both `agentArtifact.url` (control-plane init download, deploy/charts/lazycloud/templates/_helpers.tpl:478) and `awsCapacity.agentArtifactUrl` (EC2 bootstrap URL, deploy/charts/lazycloud/templates/_helpers.tpl:508), but the test only sets `agentArtifact.url` (tests/deployment/test_helm_lazycloud_chart.py:201-203). Production is right; the test is stale. The invariants it guards (provider ServiceAccount identity, worker digest pinning, route auth key by secretKeyRef, no TAILNET_AUTH_KEY in the control plane env — lines 234-241) are worth keeping.
  - **How:** Add `--set-string awsCapacity.agentArtifactUrl=<same release URL>` to the helm argument list at tests/deployment/test_helm_lazycloud_chart.py:204 so the render succeeds, then leave the existing identity/secret assertions unchanged.
- [x] `test_helm_database_bootstrap_uses_the_current_schema_only` — verdict: **update** — Most of it duplicates test_helm_one_shot_jobs_change_identity_with_their_immutable_specs (tests/deployment/test_helm_lazycloud_chart.py:301-406), which already proves the database-bootstrap Job's spec-hash name, 12-char hash, ttlSecondsAfterFinished=600 and absence of helm.sh/hook. What remains is a generated-command literal (`command == ['lazycloud-admin','--json','database','initialize']`, lines 283-288) and a repository-policy grep of the rendered text for 'backup'/'database upgrade'/'database restore' (lines 296-298) — both on the do-not-test list. The only unique material assertion is `automountServiceAccountToken is False` on the bootstrap pod (line 281).
  - **How:** Reduce to the unique security invariants: the bootstrap pod runs with automountServiceAccountToken false and no ServiceAccount/Role/RoleBinding is rendered for it (lines 280-295). Delete the command-literal assertion (283-288), the duplicated hash/name/ttl/hook assertions (266-279), and the render-text policy greps (296-298).
- [x] `test_helm_one_shot_jobs_change_identity_with_their_immutable_specs` — verdict: keep
- [x] `test_helm_chart_can_resolve_a_separate_image_archive_backend` — verdict: keep
- [x] `test_helm_chart_isolates_two_local_releases` — verdict: keep
- [x] `test_helm_default_requires_external_configuration_without_rendering_secrets` — verdict: keep
- [x] `test_helm_secret_rotation_changes_long_running_pod_templates` — verdict: keep
- [x] `test_helm_chart_derives_scaler_and_rbac_from_bounded_pool_policy` — verdict: keep
- [x] `test_helm_chart_keeps_advertised_gpu_and_pod_resources_exact` — verdict: keep
- [x] `test_helm_chart_rejects_incoherent_worker_pool_policy[overrides0-minWorkers <= initialWorkers <= maxWorkers]` — verdict: keep
- [x] `test_helm_chart_rejects_incoherent_worker_pool_policy[overrides1-must advertise zero GPUs]` — verdict: keep
- [x] `test_helm_chart_renders_platform_autoscaling_and_disruption_controls` — verdict: **update** — Dominated by constants and passthrough tautologies: it re-asserts values.yaml defaults (minReplicas 2, maxReplicas 10, averageUtilization 70, minAvailable 1, terminationGracePeriodSeconds 60 — tests/deployment/test_helm_lazycloud_chart.py:900-942), echoes back the topologySpreadConstraints it just set on the command line (set at 861-865, asserted at 903-909), and pins generated command literals for container args and the liveness/readiness probes (891-897, 914-929). The material invariants are the HPA-vs-static-replicas conflict ('replicas' absent when autoscaling is enabled, lines 898 and 913) and the scaling-owner annotations that stop another controller from fighting the scheduler pool controller (948-962).
  - **How:** Keep the render plus the 'replicas' not in spec assertions and the lazycloud.io/scaling-owner annotations; drop the values.yaml default echoes (937-942, 900-902), the topologySpreadConstraints passthrough (903-909), the args/probe command literals (891-897, 914-929), and the HPA/PDB name inventory (874-885).

### `tests/deployment/test_kubernetes_provider_scaler.py`

- [x] `test_replica_scaler_requests_bounded_desired_state_with_resource_version` — verdict: keep
- [x] `test_replica_scaler_reuses_authoritative_desired_state_without_another_patch` — verdict: keep
- [x] `test_replica_scaler_reports_at_limit_without_touching_kubernetes[0]` — verdict: keep
- [x] `test_replica_scaler_reports_at_limit_without_touching_kubernetes[5]` — verdict: keep
- [x] `test_replica_scaler_classifies_owned_kubernetes_failures[error0-unsupported-None]` — verdict: keep
- [x] `test_replica_scaler_classifies_owned_kubernetes_failures[error1-temporarily_unavailable-1.0]` — verdict: keep
- [x] `test_replica_scaler_classifies_owned_kubernetes_failures[error2-temporarily_unavailable-1.0]` — verdict: keep
- [x] `test_replica_scaler_classifies_wrongly_owned_deployment_as_unsupported` — verdict: keep
- [x] `test_replica_scaler_treats_cas_conflict_as_retryable_without_reporting_success` — verdict: keep
- [x] `test_replica_scaler_rejects_scale_response_that_did_not_retain_target` — verdict: keep
- [x] `test_scale_target_rejects_invalid_pool_bounds_identity_and_ambiguous_truncation` — verdict: keep
- [x] `test_kubernetes_api_describes_authoritative_deployment_and_cas_scales` — verdict: keep
- [x] `test_kubernetes_api_refuses_non_helm_or_wrong_pool_deployment[deployment0-app.kubernetes.io/managed-by]` — verdict: keep
- [x] `test_kubernetes_api_refuses_non_helm_or_wrong_pool_deployment[deployment1-app.kubernetes.io/component]` — verdict: keep
- [x] `test_kubernetes_api_refuses_non_helm_or_wrong_pool_deployment[deployment2-lazycloud.io/worker-pool]` — verdict: keep
- [x] `test_kubernetes_api_refuses_non_helm_or_wrong_pool_deployment[deployment3-lazycloud.io/scaling-owner]` — verdict: keep
- [x] `test_kubernetes_api_refuses_non_helm_or_wrong_pool_deployment[deployment4-capacity-owner-id]` — verdict: keep
- [x] `test_kubernetes_api_refuses_non_helm_or_wrong_pool_deployment[deployment5-metadata.name]` — verdict: keep
- [x] `test_kubernetes_api_refuses_non_helm_or_wrong_pool_deployment[deployment6-metadata.namespace]` — verdict: keep
- [x] `test_kubernetes_api_rejects_incomplete_scale_response[payload0]` — verdict: keep
- [x] `test_kubernetes_api_rejects_incomplete_scale_response[payload1]` — verdict: keep
- [x] `test_kubernetes_api_uses_authenticated_kubeconfig_fallback` — verdict: **delete** — Mock transcript of constructor wiring: it monkeypatches the third-party kubernetes config loaders and asserts the call order list `calls == ['in-cluster', 'kubeconfig']` (tests/deployment/test_kubernetes_provider_scaler.py:504-522). No state, response, error, side effect, or cleanup is observed — only that one library function is called after another raises. That is explicitly on the do-not-test list (call order, constructor wiring, third-party defaults), and the real credential path is proven by running the scheduler against a cluster.

### `tests/deployment/test_local_env_example.py`

- [x] `test_connected_object_store_does_not_retarget_local_garage_bootstrap` — verdict: **update** — Environment-dependent: `_render_compose` shells out to `docker compose ... config` with check=True and no availability guard (tests/deployment/test_local_env_example.py:36-45), so on any host without Docker the test raises FileNotFoundError instead of skipping — unlike the sibling Helm tests which guard with `shutil.which('helm')` (tests/deployment/test_helm_lazycloud_chart.py:26-28). It also asserts local Garage credential/bucket literals (lines 81-86) that are dev-stack defaults rather than production behaviour; the invariant worth keeping is only that a connected object store reaches the control plane without retargeting the local bootstrap.
  - **How:** Guard with `shutil.which('docker')`/`pytest.skip` before rendering, and narrow the assertions to the actual invariant: object-store-bucket keeps the in-cluster Garage endpoint while control-plane receives the connected bucket/endpoint (lines 84-91). Drop the GARAGE_DEFAULT_* literal assertions (81-83).

### `tests/deployment/test_worker_image_build.py`

- [x] `test_sandbox_supervisor_cross_build_uses_native_go_toolchain` — verdict: **delete** — Pure repository scan: it regex-greps docker/Dockerfile.worker for the literal strings 'ARG TARGETARCH' and 'CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH}' (tests/deployment/test_worker_image_build.py:7-18). It never builds or runs anything, so it proves source shape rather than production behaviour, and tests/CLAUDE.md states deployment tests must 'exercise real boundaries, not repository scans'. A wrong toolchain is caught by the real cross-build (deploy image build), not by this text match.

### `tests/integration/customer_compute/test_agent_cli_install.py`

- [x] `test_agent_install_join_redaction_and_summary` — verdict: **update** — Four of the five blocks fail the gate: a generated-command literal (`command[:4] == [ADMIN_CLI_NAME,'agent','join','--name']`, tests/integration/customer_compute/test_agent_cli_install.py:86-87), a vacuous truthiness check (`assert plan.service_commands`, line 89), a trivial counting helper (summarize_agent_status, lines 101-103), and a pure tautology that constructs AgentTransportEnvelope and asserts its own input back (`envelope.payload['path'] == '/agent/agent_1'`, lines 111-116). Only `redact_telemetry` (91-99) proves a material invariant — that bearer tokens, AWS secrets, api keys and passwords never reach agent telemetry.
  - **How:** Keep only the redact_telemetry block (lines 91-99) and rename the test after the secret-redaction invariant; delete lines 78-89 and 101-116.
- [x] `test_agent_install_writes_token_config_service_and_runs_commands` — verdict: keep
- [x] `test_agent_install_launchd_content_and_unsupported_platform` — verdict: **update** — Asserts the literal content of a generated launchd plist — `<string>gg.lazycloud.lazycloud-agent-private-pool</string>`, `<key>ProgramArguments</key>` (tests/integration/customer_compute/test_agent_cli_install.py:168-171) — plus the literal reason string 'unsupported operating system' (line 180). Generated plans and literal copy are on the do-not-test list. The one material claim is that the macOS plan passes the join token by file (`--join-token-file`, line 172) rather than inline, and that an unsupported OS produces a non-installing outcome.
  - **How:** Assert the outcome instead of the copy: the plan's rendered service content contains no raw join token and references the token file path, and the freebsd plan's state is Unsupported with supported False. Drop the plist key/label literal assertions (168-171) and the exact reason-string comparison (180).

### `tests/integration/customer_compute/test_agent_installer.py`

- [x] `test_install_script_runs_foreground_external_agent_without_leaking_token` — verdict: keep
- [x] `test_install_script_forwards_provider_identity_without_join_token` — verdict: keep
- [x] `test_install_script_verifies_pinned_versioned_agent_before_replacement` — verdict: keep
- [x] `test_install_script_rejects_agent_artifact_digest_mismatch` — verdict: keep
- [x] `test_install_script_refuses_missing_docker_when_install_is_disabled` — verdict: **update** — Currently fails (rc 0, expected 1) because it is host-dependent, not because production changed. `_linux_environment(minimal_path=True)` still appends /usr/bin:/bin to PATH (tests/integration/customer_compute/test_agent_installer.py:993-1000), so on a developer host `command -v docker` and `docker info` both succeed and `ensure_docker` returns before the refusal branch (packages/agent/src/agent/operations.py:455-456, 491-506). The refusal itself is intact and worth proving: with --no-install-docker and no Docker, the installer must exit 1 without leaking the join token.
  - **How:** Make the installer PATH hermetic: build a bin directory containing only the fakes plus symlinks to the coreutils the script needs (sh, sed, awk, mktemp, id, uname), and pass exactly that as PATH so no host Docker can resolve. Then keep the existing rc==1 / message / token-not-leaked assertions (lines 323-325).
- [x] `test_install_script_installs_pinned_tailscale_client_and_daemon` — verdict: **update** — Currently fails with 'Tailscale 1.98.8 was installed but failed its version readiness check'. Traced with sh -x: `tailscale version` returns 1.98.9 — the host's real /usr/bin/tailscale — because the test's PATH appends /usr/bin:/bin (line 393) and the pre-install lookup resolves and caches the host binary before the fake `install` writes the pinned stub into fake_bin. `tailscale_ready` compares the first version line (packages/agent/src/agent/operations.py:556-559), so the result depends on whether the machine happens to have Tailscale installed. The invariant — pinned archive URL, SHA-256 verification, pinned client and daemon installed — is supply-chain material and must be kept.
  - **How:** Use a hermetic PATH containing only fake_bin plus symlinks to the specific utilities the script needs, so no host tailscale/tailscaled can resolve at any point; keep the archive-URL, sha256 and installed-binary assertions (lines 416-424) unchanged.
- [x] `test_install_script_fails_before_changes_when_background_is_not_root` — verdict: keep
- [x] `test_background_installer_waits_for_runtime_ready_marker_and_active_service` — verdict: keep
- [x] `test_background_installer_fails_with_redacted_service_diagnostics` — verdict: keep
- [x] `test_service_dry_run_never_serializes_raw_join_token` — verdict: keep
- [x] `test_status_validates_state_and_never_outputs_agent_token` — verdict: keep
- [x] `test_leave_removes_service_and_private_state` — verdict: keep
- [x] `test_remote_leave_authenticates_with_saved_machine_credential` — verdict: keep
- [x] `test_leave_cache_destruction_receipt_survives_gateway_retry` — verdict: keep
- [x] `test_remote_leave_sends_exact_cache_destruction_session` — verdict: keep
- [x] `test_remote_leave_treats_revoked_credential_as_already_absent` — verdict: keep
- [x] `test_remote_leave_surfaces_upstream_failure_and_preserves_local_state` — verdict: keep
- [x] `test_leave_refuses_local_cleanup_when_saved_identity_is_missing` — verdict: keep

### `tests/integration/customer_compute/test_backend_route_dialer.py`

- [x] `test_backend_route_dialer_waits_for_ready_route_and_writes_preface` — verdict: keep
- [x] `test_backend_route_dialer_transport_retry_matrix[agent.tailnet:29443-tsnet_restricted-1-100.64.0.2-100.64.0.2:29443]` — verdict: keep
- [x] `test_backend_route_dialer_transport_retry_matrix[100.64.0.10:29443-tsnet_restricted-0-100.64.0.99-100.64.0.10:29443]` — verdict: keep
- [x] `test_backend_route_dialer_transport_retry_matrix[10.0.0.5:8000-direct-0-unused-10.0.0.5:8000]` — verdict: keep
- [x] `test_backend_route_dialer_transport_retry_matrix[container-worker:57267-direct-1-unused-container-worker:57267]` — verdict: keep
- [x] `test_backend_route_dialer_rejects_missing_authenticator_before_proxying` — verdict: keep
- [x] `test_backend_route_dialer_rejects_unusable_routes` — verdict: keep
- [x] `test_shell_backend_uses_authoritative_route_with_raw_address` — verdict: keep
- [x] `test_shell_backend_authenticates_tailnet_route_before_shell_protocol` — verdict: keep

### `tests/integration/customer_compute/test_compute_agent_control.py`

- [x] `test_join_token_binding_and_agent_join_gpu_locking` — verdict: keep

### `tests/integration/customer_compute/test_compute_machine_enrollment.py`

- [x] `test_machine_enrollment_is_durable_rotatable_and_secret_free` — verdict: keep
- [x] `test_capacity_interruption_is_session_fenced_durable_and_heartbeat_safe` — verdict: keep
- [x] `test_agent_leave_cleans_up_and_public_delete_requires_host_decommission` — verdict: keep
- [x] `test_agent_leave_requires_current_machine_cache_destruction_session` — verdict: keep
- [x] `test_pool_delete_requires_host_decommission_without_mutating_ownership` — verdict: keep
- [x] `test_workspace_deletion_preflight_preserves_enrolled_self_hosted_ownership` — verdict: keep
- [x] `test_telemetry_usage_failure_does_not_advance_enrollment_cursor` — verdict: keep
- [x] `test_issuing_a_new_join_command_revokes_the_previous_credential` — verdict: keep
- [x] `test_machine_join_command_owns_the_workspace_self_hosted_fleet` — verdict: keep

### `tests/integration/customer_compute/test_compute_provider_billing_metrics.py`

- [x] `test_compute_launch_reconcile_billing_and_termination` — verdict: **update** — Stale after production gained bootstrap-phase reclaim. The test reconciles at now+10 minutes and expects the reservation to still be active/pending (tests/integration/customer_compute/test_compute_provider_billing_metrics.py:73-75), but a machine that never produces an available worker is reclaimed after the 300-second phase deadline (packages/compute/src/compute/reclaim.py:43-48; packages/compute/src/compute/service.py:4884-4898), so status is already 'deleted' and the later credit-exhaustion assertions no longer prove credit-driven termination. Production is correct.
  - **How:** Run the first reconcile inside the bootstrap window (e.g. now+2 minutes) for the active/pending plus usage-recorded assertions, then set the insufficient-credits decision and reconcile again still inside the window so the 'deleted' status at line 87 is attributable to credit exhaustion rather than the bootstrap deadline.
- [x] `test_compute_rejects_unavailable_provider_and_mixed_gpu_pool` — verdict: keep
- [x] `test_gateway_maps_provider_offer_discovery_failure_to_503` — verdict: keep
- [x] `test_gateway_rejects_offer_discovery_without_configured_providers` — verdict: keep

### `tests/integration/customer_compute/test_gateway_tailnet_lifecycle.py`

- [x] `test_resource_pool_delete_runs_canonical_cleanup_and_rescans_late_device` — verdict: keep
- [x] `test_resource_pool_delete_requires_host_leave_before_mutation` — verdict: keep
- [x] `test_resource_pool_delete_cannot_delete_another_workspace_pool` — verdict: keep
- [x] `test_workspace_deletion_conflicts_before_self_hosted_tailnet_authority_changes` — verdict: keep
- [x] `test_transport_credential_is_one_off_and_supersedes_unused_key` — verdict: keep
- [x] `test_verified_provider_device_is_required_and_agent_claims_are_ignored` — verdict: keep
- [x] `test_restart_reuses_verified_device_without_issuing_another_key` — verdict: keep
- [x] `test_stale_registration_cannot_restore_device_removed_by_rotation` — verdict: keep
- [x] `test_rotation_reconciles_device_created_before_registration_process_loss` — verdict: keep
- [x] `test_new_rotation_supersedes_in_flight_generation` — verdict: keep
- [x] `test_stale_rotating_generation_is_recoverable_after_process_loss` — verdict: keep
- [x] `test_failed_rotation_retains_cleanup_ownership_for_retry` — verdict: keep
- [x] `test_terminal_deletion_supersedes_in_flight_rotation` — verdict: keep
- [x] `test_terminal_deletion_cleans_abandoned_rotation_resources` — verdict: keep
- [x] `test_terminal_cleanup_discovers_unregistered_device_and_retries` — verdict: keep
- [x] `test_terminal_cleanup_retains_tombstone_for_device_visible_after_initial_scan` — verdict: keep
- [x] `test_terminal_hostname_reconciliation_is_machine_scoped` — verdict: keep
- [x] `test_machine_and_pool_deletion_remove_tailnet_identity_and_key` — verdict: keep
- [x] `test_cleanup_revokes_platform_authority_before_retryable_device_removal` — verdict: keep

### `tests/integration/customer_compute/test_private_pool_cleanup.py`

- [x] `test_delete_pool_cleans_private_agent_state` — verdict: **update** — Fails at tests/integration/customer_compute/test_private_pool_cleanup.py:187 because the test's own setup is wrong, not production: the SchedulerWorkerRecord is registered under a hardcoded foreign capacity owner '11111111-1111-4111-8111-111111111111' (lines 150-163) while `_delete_pool_workers` correctly removes only workers whose capacity_owner_id matches the deleted pool (packages/gateway/src/gateway/service.py:1515-1520). Asserting that a foreign owner's worker is deleted would demand a cross-tenant deletion. The scoped-cleanup invariant is worth keeping.
  - **How:** Register the worker (and the ComputeAgentWorkerSlotState at lines 130-138) with `pool.capacity_owner_id`. Better: register two workers — one owned by the pool, one under a foreign capacity owner — and assert only the owned one is removed, which turns the assertion into a genuine scoped-cleanup proof.
- [x] `test_delete_pool_removes_durable_pool_when_redis_projection_is_missing` — verdict: keep
- [x] `test_delete_pool_retains_durable_ownership_when_provider_cleanup_fails` — verdict: keep

### `tests/integration/customer_compute/test_tailnet_cleanup.py`

- [x] `test_tombstone_survives_ownership_deletion_and_scheduler_removes_late_device` — verdict: keep
- [x] `test_new_schedule_revision_supersedes_in_flight_cleanup_claim` — verdict: keep
- [x] `test_scheduler_reports_pending_cleanup_when_control_credentials_are_unavailable` — verdict: keep
- [x] `test_postgresql_concurrent_first_schedules_merge_and_supersede_claim` — verdict: keep

### `tests/integration/test_autoscaler_operations.py`

- [x] `test_autoscaler_cli_controls_real_api_and_persists_owner_state` — verdict: keep

### `tests/integration/test_container_inspection.py`

- [x] `test_canonical_container_pages_are_bounded_stable_and_secret_free` — verdict: keep
- [x] `test_sdk_container_client_preserves_page_request_and_rejects_environment_data` — verdict: keep

### `tests/integration/test_container_scheduling.py`

- [x] `test_container_scheduling_failure_syncs_container_and_task` — verdict: keep
- [x] `test_function_invoke_requests_workspace_storage_when_workspace_bucket_available` — verdict: keep
- [x] `test_function_dependency_waits_then_schedules_materialized_args` — verdict: keep
- [x] `test_function_result_and_completion_reject_stale_container_attempt` — verdict: keep
- [x] `test_function_args_require_running_current_container_across_retry_replacement` — verdict: keep
- [x] `test_function_cancel_stops_container_and_rejects_terminal_writes` — verdict: keep
- [x] `test_function_dependency_failure_fails_downstream_without_scheduling` — verdict: keep
- [x] `test_checkpoint_task_queue_runner_receives_checkpoint_barrier_env` — verdict: keep

### `tests/integration/test_container_service_transport.py`

- [x] `test_http_container_service_transport_rejects_non_socket_route_connections` — verdict: keep

### `tests/integration/test_deployed_invoke_routes.py`

- [x] `test_unversioned_invoke_rejects_stopped_latest_without_fallback` — verdict: keep
- [x] `test_private_function_deployed_routes_use_token_workspace` — verdict: keep
- [x] `test_endpoint_version_routes_follow_deployment_lifecycle` — verdict: keep
- [x] `test_endpoint_host_routing_preserves_numeric_deployment_suffixes` — verdict: keep
- [x] `test_private_endpoint_and_asgi_id_routes_use_token_workspace` — verdict: keep
- [x] `test_generated_asgi_urls_forward_subpaths_and_warmup` — verdict: keep
- [x] `test_generated_task_queue_urls_forward_to_put_and_warmup` — verdict: keep
- [x] `test_private_task_queue_deployed_routes_use_token_workspace` — verdict: keep

### `tests/integration/test_endpoint_lifecycle.py`

- [x] `test_endpoint_runner_records_handler_failure_on_request_task` — verdict: keep
- [x] `test_asgi_runner_streams_http_and_proxies_websocket_subprotocol` — verdict: keep
- [x] `test_asgi_websocket_dispatch_session_heartbeats_and_finishes` — verdict: keep
- [x] `test_endpoint_service_without_running_container_schedules_warmup` — verdict: keep
- [x] `test_endpoint_service_waits_for_warm_capacity_before_dispatch` — verdict: keep
- [x] `test_endpoint_and_asgi_reject_before_creating_runs_when_request_buffer_is_full` — verdict: keep
- [x] `test_asgi_websocket_rejects_before_creating_run_when_request_buffer_is_full` — verdict: keep
- [x] `test_endpoint_service_ignores_stale_dispatch_records_for_backpressure` — verdict: keep
- [x] `test_endpoint_service_cancelled_request_stops_waiting_for_capacity` — verdict: keep

### `tests/integration/test_function_runner_streaming.py`

- [x] `test_function_runner_streams_plain_user_logs_and_persists_result` — verdict: keep
- [x] `test_function_runner_rejects_untyped_invocation_envelopes` — verdict: keep
- [x] `test_function_runner_failure_streams_and_persists_traceback` — verdict: keep
- [x] `test_task_log_stream_flush_publishes_partial_line_once` — verdict: keep

### `tests/integration/test_pod_deployment_scale_api.py`

- [x] `test_pod_replica_scaling_is_typed_authorized_and_lifecycle_gated` — verdict: keep
- [x] `test_pod_scale_rejects_incompatible_checkpoint_before_mutation` — verdict: keep

### `tests/integration/test_pod_sandbox_proxy_routes.py`

- [x] `test_pod_id_proxy_preserves_request_and_selects_port_ready_container` — verdict: keep
- [x] `test_pod_proxy_records_demand_before_waiting_for_scale_from_zero` — verdict: keep
- [x] `test_pod_websocket_proxies_subprotocol_text_binary_and_balances_demand[pod-private]` — verdict: keep
- [x] `test_pod_websocket_proxies_subprotocol_text_binary_and_balances_demand[sandbox-private]` — verdict: keep
- [x] `test_pod_websocket_proxies_subprotocol_text_binary_and_balances_demand[sandbox-public]` — verdict: keep
- [x] `test_pod_websocket_proxies_subprotocol_text_binary_and_balances_demand[sandbox-host]` — verdict: keep
- [x] `test_pinned_sandbox_routes_never_wait_or_fall_through_to_a_sibling` — verdict: keep
- [x] `test_pinned_sandbox_route_metadata_is_ready_exact_and_address_bound` — verdict: keep
- [x] `test_pinned_sandbox_backend_failures_are_bounded_and_typed` — verdict: keep
- [x] `test_sandbox_proxy_supports_id_deployment_and_public_path_forms` — verdict: keep
- [x] `test_pod_proxy_returns_service_unavailable_when_port_is_missing` — verdict: keep
- [x] `test_pod_and_sandbox_private_routes_use_token_workspace` — verdict: keep
- [x] `test_cross_workspace_public_app_does_not_publish_a_private_sandbox` — verdict: keep

### `tests/integration/test_pod_shell_remote.py`

- [x] `test_ephemeral_pod_create_overrides_command_returns_url_and_expires` — verdict: keep
- [x] `test_pod_api_schedules_container_and_routes_exec_and_files_to_worker` — verdict: keep
- [x] `test_existing_container_shell_reuses_credentials_through_worker_client` — verdict: keep
- [x] `test_existing_container_shell_rejects_unrelated_listener_and_rolls_back_port` — verdict: keep
- [x] `test_existing_container_ticket_failure_unpublishes_listener_idempotently` — verdict: keep
- [x] `test_standalone_ticket_failure_stops_once_and_terminal_retry_is_idempotent` — verdict: keep
- [x] `test_standalone_ticket_cleanup_failure_preserves_truth_and_records_safe_event` — verdict: keep
- [x] `test_sandbox_exec_waits_for_worker_address_before_dial` — verdict: keep
- [x] `test_sandbox_connect_requires_supervisor_readiness` — verdict: keep
- [x] `test_sandbox_connect_surfaces_terminal_scheduler_state_as_conflict` — verdict: keep
- [x] `test_shell_websocket_proxies_bidirectional_terminal_bytes` — verdict: keep
- [x] `test_shell_websocket_rejects_long_lived_query_credentials_before_backend` — verdict: keep

### `tests/integration/test_process_entrypoints.py`

- [x] `test_scheduler_runtime_closes_owned_services_on_exception` — verdict: keep
- [x] `test_scheduler_app_services_stop_cancels_the_real_scheduler_request` — verdict: keep
- [x] `test_container_worker_process_deregisters_on_shutdown_signal` — verdict: keep
- [x] `test_container_worker_process_deregisters_when_startup_after_registration_fails` — verdict: keep
- [x] `test_container_worker_process_spins_down_idle_nonpersistent_worker` — verdict: keep

### `tests/integration/test_production_data_plane.py`

- [x] `test_deployment_versions_continue_after_soft_delete` — verdict: keep
- [x] `test_active_app_names_are_unique_per_workspace` — verdict: keep
- [x] `test_s3_presign_endpoint_matches_storage_endpoint_policy` — verdict: keep
- [x] `test_s3_delete_objects_requests_include_content_md5` — verdict: keep

### `tests/integration/test_resource_workspace_canonicalization.py`

- [x] `test_resource_routes_canonicalize_workspace_name_and_id` — verdict: keep

### `tests/integration/test_sandbox_lifecycle.py`

- [x] `test_sandbox_create_refresh_and_terminate_own_the_durable_ttl_lock` — verdict: keep
- [x] `test_scheduler_expires_prepared_sandbox_without_a_deployment` — verdict: keep
- [x] `test_sandbox_restore_resolves_source_stub_and_schedules_typed_checkpoint` — verdict: keep

### `tests/integration/test_source_code_mounts.py`

- [x] `test_source_code_mounts_presign_for_the_source_workspace` — verdict: keep
- [x] `test_object_storage_deletes_only_the_target_workspace_objects` — verdict: keep
- [x] `test_object_storage_deletes_each_workspace_physical_object_independently` — verdict: keep
- [x] `test_object_storage_preserves_metadata_when_physical_delete_is_not_confirmed` — verdict: keep
- [x] `test_container_resource_mounts_require_workspace_storage_when_workspace_has_bucket` — verdict: keep
- [x] `test_platform_volume_local_path_rejects_namespace_traversal[../workspace-volume-1]` — verdict: keep
- [x] `test_platform_volume_local_path_rejects_namespace_traversal[workspace-1-../volume]` — verdict: keep
- [x] `test_platform_volume_local_path_rejects_namespace_traversal[workspace/other-volume-1]` — verdict: keep
- [x] `test_platform_volume_local_path_rejects_namespace_traversal[workspace-1-volume\\other]` — verdict: keep
- [x] `test_worker_source_materializer_extracts_isolated_container_workspaces` — verdict: keep
- [x] `test_worker_source_materializer_purges_only_requested_workspace_objects` — verdict: keep

### `tests/integration/test_tcp_ingress.py`

- [x] `test_tcp_route_resolver_uses_active_public_pod_hierarchy_and_revalidates_cache` — verdict: keep
- [x] `test_tcp_route_resolver_rejects_private_non_tcp_and_unexposed_pods` — verdict: keep
- [x] `test_tls_context_captures_sni_and_reloads_atomically_rotated_certificate` — verdict: keep

### `tests/integration/test_worker_repository_service.py`

- [x] `test_image_build_credentials_reject_wrong_assigned_worker` — verdict: keep
- [x] `test_automatic_checkpoint_lease_is_bound_to_assigned_container_and_worker` — verdict: keep
- [x] `test_managed_image_build_credentials_use_assigned_workspace` — verdict: keep
- [x] `test_managed_container_credentials_use_assigned_workspace` — verdict: keep
- [x] `test_image_archive_upload_credentials_are_bound_and_one_time` — verdict: keep
- [x] `test_image_build_context_download_is_bound_to_active_assignment_and_object` — verdict: keep
- [x] `test_cache_origin_broker_returns_urls_without_storage_credentials` — verdict: keep
- [x] `test_cache_origin_broker_denies_other_workers_container_and_image` — verdict: keep
- [x] `test_worker_repository_api_authenticates_and_streams_container_requests` — verdict: **update** — Fails with "attached compute pool 'pool' does not support the requested resources" at the shared helper tests/integration/test_worker_repository_service.py:2471. The pool is created with `create_pool('pool')`, whose worker capacity defaults to 0 millicores / 0 MiB (packages/compute/src/compute/service.py:1300-1301), so the pinned 100m/128MiB request is rejected by `_pool_supports` (packages/compute/src/compute/request_placement.py:150-155). Production's pool-capability precheck is correct; the test setup predates it. The worker-token authentication and request-stream assertions are a real authorization boundary and must be kept.
  - **How:** Create the pool with advertised worker capacity matching the registered worker, e.g. `isolated_services.compute.create_pool('pool', worker_cpu_millicores=1000, worker_memory_mib=1024)` at line 862, so submit is accepted; leave the auth/stream assertions unchanged.
- [x] `test_worker_network_mutations_are_bound_to_authenticated_worker_assignment` — verdict: keep
- [x] `test_worker_repository_stream_blocks_until_scheduler_assignment` — verdict: **update** — Same stale setup as the sibling test: the 'default' pool is created with zero advertised worker capacity (tests/integration/test_worker_repository_service.py:1050) so `_pool_supports` rejects the 100m/128MiB pinned request (packages/compute/src/compute/request_placement.py:150-155) and the helper assertion at line 2471 fails. The invariant under test — the worker stream blocks until the scheduler assigns, then delivers exactly once — is a concurrency guarantee worth keeping.
  - **How:** Create the pool with `worker_cpu_millicores`/`worker_memory_mib` at least as large as the submitted request (matching the registered worker's 1000/1024) at line 1050; no change to the blocking-stream assertions.
- [x] `test_stale_source_cache_session_cannot_change_current_worker_availability` — verdict: keep
- [x] `test_worker_stream_rechecks_cache_after_dequeue_and_requeues_on_drain` — verdict: keep
- [x] `test_worker_repository_api_vends_container_credentials_from_worker_token` — verdict: keep
- [x] `test_worker_repository_rotates_worker_session_on_reregistration` — verdict: keep
- [x] `test_worker_registration_fails_closed_without_matching_durable_capacity_owner` — verdict: keep
- [x] `test_container_shutdown_owner_confirms_targeted_worker_ack` — verdict: keep
- [x] `test_container_shutdown_owner_accepts_concurrent_scheduler_completion` — verdict: keep
- [x] `test_container_shutdown_owner_accepts_unassigned_pending_cancellation` — verdict: keep
- [x] `test_container_shutdown_owner_rejects_optimistic_database_terminal_state` — verdict: keep
- [x] `test_container_shutdown_owner_rejects_cross_worker_shutdown_ack` — verdict: keep
- [x] `test_worker_repository_filters_targeted_stop_events_by_assigned_worker` — verdict: keep
- [x] `test_worker_repository_service_persists_checkpoint_archive_and_state` — verdict: keep
- [x] `test_worker_repository_lifecycle_failure_marks_container_and_task_failed` — verdict: keep
- [x] `test_worker_repository_exit_preserves_function_retry_state` — verdict: keep
- [x] `test_worker_repository_stale_container_exit_does_not_fail_new_attempt` — verdict: keep
- [x] `test_worker_repository_late_exit_preserves_user_stopped_container` — verdict: keep
- [x] `test_worker_repository_container_cleanup_unpublishes_every_port_route` — verdict: keep
- [x] `test_worker_repository_reconciles_orphan_routes_without_removing_active_routes` — verdict: keep
- [x] `test_agent_route_status_update_reconciles_scheduler_backend_route` — verdict: **update** — The assertions pass but the test errors in teardown after exactly 10s: marking the route Ready with proxy_target 'tailnet-host:34399' (tests/integration/test_worker_repository_service.py:2264-2270) launches a real prewarm dial to an unresolvable host, and `ApiServices.close()` raises 'route prewarm shutdown timed out with 1 active operation(s)'. That exposes a production defect (see production_bugs), and the test also needs changing: it lets the API graph make a real outbound dial, which is non-deterministic and machine-dependent.
  - **How:** Point proxy_target at a bounded, deterministic target (a loopback listener the test owns, or a stub prewarm runner/dialer injected into the replaced gateway service at lines 2228-2236) so the prewarm completes instead of blocking; keep the route-resolution assertions at 2277-2280.
  - **PRODUCTION BUG:** Route prewarm can never be shut down cleanly while a dial is in flight. ThreadRoutePrewarmRunner.shutdown_timeout_seconds is 10.0 (packages/gateway/src/gateway/route_prewarm.py:62) but the backend route dial timeout is 30.0 (packages/networking/src/networking/dialer.py:21), and the tsnet peer wait consumes the same budget (packages/networking/src/networking/dialer.py:89, 167-168). close() therefore raises RuntimeError('route prewarm shutdown timed out with N active operation(s)') at packages/gateway/src/gateway/route_prewarm.py:95-97, which propagates through RoutePrewarmService.close() (line 187) into ApiServices.close() and becomes ExceptionGroup('API service shutdown was incomplete') at apps/api/src/api/server/services.py:1079/1119. Reproduced deterministically: a single route marked Ready with an unreachable proxy_target makes the API shutdown fail after exactly 10.19s. Any control-plane restart while a route is prewarming an unreachable or slow backend fails its shutdown path. The shutdown budget must be at least the dial budget, or the prewarm dial must be cancellable/bounded by the shutdown deadline.

### `tests/test_suite_environment.py`

- [x] `test_suite_does_not_inherit_developer_configuration` — verdict: **delete** — Tests the suite's own machinery, not production. It asserts the effects of the root autouse fixture `isolated_environment` (conftest.py:39-57), i.e. that the harness cleared LAZYCLOUD_*/AWS_* and applied tests/env.test (tests/test_suite_environment.py:8-30). Gate criterion 1 excludes test machinery/fixtures, and the assertion is vacuous on any host that has no developer `.env` (CI), so it also fails to protect what its docstring claims wherever the risk does not exist.

---

## Opt-in E2E (`tests/e2e`)

Not collected by pytest. Scenarios are production capability proofs, not
unit tests: judge each on whether it proves a distinct user-visible
capability through a public surface and cleans up after itself.

- [x] `tests/e2e/_support/process.py` — verdict: keep
- [x] `tests/e2e/external/_support.py` — verdict: keep
- [x] `tests/e2e/external/aws/account_connection.py` — verdict: keep
- [x] `tests/e2e/external/aws/cleanup.py` — verdict: keep
- [x] `tests/e2e/external/aws/connected_aws_function.py` — verdict: keep
- [x] `tests/e2e/external/aws/one_machine_readiness.py` — verdict: keep
- [x] `tests/e2e/external/gpu/image_build.py` — verdict: keep
- [x] `tests/e2e/external/gpu/workloads.py` — verdict: keep
- [x] `tests/e2e/external/tailnet/_tailscale.py` — verdict: keep
- [x] `tests/e2e/external/tailnet/agent_join.py` — verdict: keep
- [x] `tests/e2e/external/tailnet/cleanup.py` — verdict: **delete** — Teardown orchestration that deletes one app by id with a name-prefix guard (tests/e2e/external/tailnet/cleanup.py:34-45). It proves no capability and duplicates both the public owner `lazycloud app delete` (packages/lazycloud/src/lazycloud/cli/apps.py:71) and the deletion plus residue verification the owning scenario already performs on every path (tests/e2e/external/tailnet/endpoint_route.py:68 and :113-124). The independently-callable-cleanup exemption in tests/e2e/AGENTS.md:26-27 is about paid capacity and zero-cost proof (tests/e2e/external/aws/cleanup.py:158-213), not a plain app delete.
- [x] `tests/e2e/external/tailnet/endpoint_route.py` — verdict: keep
- [x] `tests/e2e/external/tailnet/preflight.py` — verdict: **delete** — Pure environment inventory: tailscale CLI node id/online (tests/e2e/external/tailnet/preflight.py:36-40) plus a gateway /health probe (:42-45). No LazyCloud production capability is exercised. The prepared Tailnet is proven functional by tests/e2e/external/tailnet/agent_join.py:39-53 (guarded worker Available on the tsnet_restricted pool) and tests/e2e/external/tailnet/endpoint_route.py:46-57 (Endpoint executes on the exact Tailnet worker); node identity is re-checked where it matters at tests/e2e/external/tailnet/restart_continuity.py:77-79. The shared helper tests/e2e/external/tailnet/_tailscale.py stays - restart_continuity.py:14 still uses it.
- [x] `tests/e2e/external/tailnet/restart_continuity.py` — verdict: keep
- [x] `tests/e2e/external/tailnet/workloads.py` — verdict: keep
- [x] `tests/e2e/function_round_trip.py` — verdict: **delete** — Duplicate Function round trip that also sits outside the local/external layout required by tests/e2e/AGENTS.md:5-6, and bypasses the shared live gate (its own --run-id parsing at tests/e2e/function_round_trip.py:95-116 instead of tests/e2e/_support/process.py:41). Its evidence is a strict subset of tests/e2e/external/aws/connected_aws_function.py:302-329 (same result contract, marker retained in public task logs, container exit code, exact warm-baseline machine, plus tag-scoped AWS corroboration) for the connected provider and of tests/e2e/local/function/scenario_invoke.py:40-43 for the local provider; its pre/post compute-baseline check (function_round_trip.py:72-83) is covered by tests/e2e/external/aws/connected_aws_function.py:193-208.
- [x] `tests/e2e/function_round_trip_workload.py` — verdict: **delete** — Fixture with a single consumer, tests/e2e/function_round_trip.py:22; orphaned once that scenario is removed. The equivalent minimal probe already lives beside its own consumer at tests/e2e/local/function/workload_invoke.py:11-13.
- [x] `tests/e2e/local/authorization/scenario_device_login.py` — verdict: keep
- [x] `tests/e2e/local/cache/scenario_restart.py` — verdict: keep
- [x] `tests/e2e/local/cache/workload_restart.py` — verdict: keep
- [x] `tests/e2e/local/checkpoints/endpoint_restore.py` — verdict: keep
- [x] `tests/e2e/local/checkpoints/pod_restore.py` — verdict: keep
- [x] `tests/e2e/local/checkpoints/pod_server.py` — verdict: keep
- [x] `tests/e2e/local/checkpoints/workloads.py` — verdict: keep
- [x] `tests/e2e/local/compose/readiness.py` — verdict: **delete** — Platform inventory rather than a capability: it lists pools, machines and workers and asserts the Compose stack's default pool is agent-provided (tests/e2e/local/compose/readiness.py:40-65). tests/e2e/AGENTS.md:4 states scenarios "never build, deploy, migrate, reset, or inventory the platform", and a failure here means the operator's environment is unprepared, not that production is defective. Any local capability scenario proves the same schedulable-worker invariant more strongly by actually executing work on it, e.g. tests/e2e/local/function/scenario_invoke.py:40-43.
- [x] `tests/e2e/local/compute_fake/agent.py` — verdict: **delete** — A fabricated node agent whose only consumer is the dev loop (tests/e2e/local/compute_fake/agent.py:1-9, imported at tests/e2e/local/compute_fake/devloop.py:74). tests/e2e/AGENTS.md:29-31 forbids fabricated agents in this tree, and the module is orphaned once devloop.py is removed. The real enrollment request/phase/failure paths it imitates are owned by packages/gateway/provider_enrollment and covered at packages/gateway/tests/test_machine_lifecycle.py:37.
- [x] `tests/e2e/local/compute_fake/devloop.py` — verdict: **delete** — Self-declared non-acceptance dev loop (tests/e2e/local/compute_fake/devloop.py:8-13 "This is a dev-only iteration loop, not an acceptance scenario"). It composes production services directly rather than driving a public SDK/CLI/API path (devloop.py:217-239), substitutes FakePooledCapacityProvider for the provider boundary (devloop.py:216), reads private PostgreSQL repositories for its evidence (devloop.py:349-359), and drops/creates a database (devloop.py:470-491) - all forbidden by tests/e2e/AGENTS.md:4 and :29-31. The bootstrap/reclaim/degrade transitions it drives are already proven at the cheaper authoritative owner with real repositories: packages/compute/tests/test_provider_capacity_reclaim.py:1381, :1418, :1248 and packages/compute/tests/test_capacity_worker_bootstrap.py:26.
- [x] `tests/e2e/local/function/scenario_cancel_rerun.py` — verdict: keep
- [x] `tests/e2e/local/function/scenario_dependency.py` — verdict: keep
- [x] `tests/e2e/local/function/scenario_invoke.py` — verdict: keep
- [x] `tests/e2e/local/function/scenario_retry.py` — verdict: keep
- [x] `tests/e2e/local/function/scenario_schedule.py` — verdict: keep
- [x] `tests/e2e/local/function/scenario_serialization.py` — verdict: keep
- [x] `tests/e2e/local/function/workload_cancel_rerun.py` — verdict: keep
- [x] `tests/e2e/local/function/workload_dependency.py` — verdict: keep
- [x] `tests/e2e/local/function/workload_invoke.py` — verdict: keep
- [x] `tests/e2e/local/function/workload_retry.py` — verdict: keep
- [x] `tests/e2e/local/function/workload_schedule.py` — verdict: keep
- [x] `tests/e2e/local/function/workload_serialization.py` — verdict: keep
- [x] `tests/e2e/local/image_build/python_package.py` — verdict: keep
- [x] `tests/e2e/local/image_build/system_package.py` — verdict: keep
- [x] `tests/e2e/local/kubernetes/chart_readiness.py` — verdict: **delete** — Verifies operator-installed Helm/kubectl state and /health (tests/e2e/local/kubernetes/chart_readiness.py:68-115) - third-party deployment wiring inventory, not LazyCloud production behaviour at a public boundary. The prepared pool's readiness is already gated inside the scenario that depends on it, which then proves the real behaviour: tests/e2e/local/kubernetes/scale_up.py:36-38 blocks unless the worker pool is ready at baseline and tests/e2e/local/kubernetes/scale_up.py:142-154 proves actual scale-up. deploy/local-cluster/README.md:37 invokes this module and must be updated in the same change.
- [x] `tests/e2e/local/kubernetes/maximum_enforcement.py` — verdict: keep
- [x] `tests/e2e/local/kubernetes/preemption.py` — verdict: keep
- [x] `tests/e2e/local/kubernetes/scale_down.py` — verdict: keep
- [x] `tests/e2e/local/kubernetes/scale_up.py` — verdict: keep
- [x] `tests/e2e/local/kubernetes/scheduler_restart.py` — verdict: keep
- [x] `tests/e2e/local/kubernetes/workloads.py` — verdict: keep
- [x] `tests/e2e/local/managed_runtime/runtime_package.py` — verdict: keep
- [x] `tests/e2e/local/pod/scenario_attach.py` — verdict: keep
- [x] `tests/e2e/local/private_registry/scenario_archive_after_loss.py` — verdict: keep
- [x] `tests/e2e/local/private_registry/scenario_authenticated.py` — verdict: keep
- [x] `tests/e2e/local/private_registry/scenario_credential_scope.py` — verdict: keep
- [x] `tests/e2e/local/private_registry/workload_archive.py` — verdict: keep
- [x] `tests/e2e/local/private_registry/workload_authenticated.py` — verdict: keep
- [x] `tests/e2e/local/private_registry/workload_scope.py` — verdict: keep
- [x] `tests/e2e/local/sandbox/scenario_compose.py` — verdict: keep
- [x] `tests/e2e/local/sandbox/scenario_docker.py` — verdict: keep
- [x] `tests/e2e/local/sandbox/scenario_exposed_port_restart.py` — verdict: keep
- [x] `tests/e2e/local/sandbox/scenario_lifecycle.py` — verdict: keep
- [x] `tests/e2e/local/sandbox/scenario_snapshot.py` — verdict: keep
- [x] `tests/e2e/local/shell/scenario_cli.py` — verdict: keep
- [x] `tests/e2e/local/shell/scenario_ticket.py` — verdict: keep
- [x] `tests/e2e/local/shell/workload_cli.py` — verdict: keep
- [x] `tests/e2e/local/shell/workload_ticket.py` — verdict: keep
- [x] `tests/e2e/local/storage/scenario_artifact.py` — verdict: keep
- [x] `tests/e2e/local/storage/scenario_cloud_bucket.py` — verdict: keep
- [x] `tests/e2e/local/storage/scenario_volume_cli.py` — verdict: keep
- [x] `tests/e2e/local/storage/scenario_volume_mount.py` — verdict: keep
- [x] `tests/e2e/local/storage/scenario_volume_transfer.py` — verdict: keep
- [x] `tests/e2e/local/storage/workload_artifact.py` — verdict: keep
- [x] `tests/e2e/local/storage/workload_cloud_bucket.py` — verdict: keep
- [x] `tests/e2e/local/storage/workload_volume_mount.py` — verdict: keep
- [x] `tests/e2e/local/usage/billing_report.py` — verdict: keep
- [x] `tests/e2e/local/usage/function_usage.py` — verdict: keep
- [x] `tests/e2e/local/usage/volume_metering.py` — verdict: keep
- [x] `tests/e2e/local/worker/enrollment_drain.py` — verdict: **update** — Stale status literals make the scenario dead and its core assertion wrong. The public contract carries SchedulerWorkerStatus values verbatim (packages/scheduler/src/scheduler/workers.py:145 sets `status=worker.status.value`; packages/shared/src/shared/scheduling.py:17-21 defines pending|available|draining|disabled), so `source.status != "ready"` at tests/e2e/local/worker/enrollment_drain.py:41 always returns exit 77 and the drain is never exercised. If it did run, tests/e2e/local/worker/enrollment_drain.py:58 would reject a correct drain: drain routes through disable_worker (packages/scheduler/src/scheduler/workers.py:122 -> packages/scheduler/src/scheduler/state.py:780-785), which durably sets Unavailable ("disabled"), not "draining". The same dead literal repeats at :84 (uncordon expectation) and :133 (replacement wait). The invariant itself - authenticated drain refuses to stop active containers, a replacement worker becomes schedulable, and uncordon restores the source - is worth keeping.
  - **How:** Replace the "ready" literals at lines 41, 84 and 133 with SchedulerWorkerStatus.Available.value, and the post-drain expectation at line 58 with SchedulerWorkerStatus.Unavailable.value; import the enum from shared.scheduling instead of hard-coding strings. Keep the rest of the flow unchanged, including the `result.stopped_container_ids` safety assertion and the uncordon cleanup path.

---

## Web (`apps/web`)

- [x] `apps/web/src/components/shared/AppShell/WorkspaceSwitcher.test.ts` — verdict: keep
- [x] `apps/web/src/components/shared/ContainerMetricsCharts/metrics.test.ts` — verdict: keep
- [x] `apps/web/src/components/shared/ErrorBoundary/index.test.tsx` — verdict: keep
- [x] `apps/web/src/components/shared/TaskDrawer/TaskTimeline/phases.test.ts` — verdict: keep
- [x] `apps/web/src/components/shared/TaskDrawer/TaskTimeline/timeline.test.ts` — verdict: keep
- [x] `apps/web/src/components/shared/ThemeProvider/index.test.tsx` — verdict: keep
- [x] `apps/web/src/components/shared/WorkspaceDeletion/controller.test.ts` — verdict: keep
- [x] `apps/web/src/components/shared/WorkspaceLiveUpdates/index.test.ts` — verdict: keep
- [x] `apps/web/src/hooks/useEventStream.test.tsx` — verdict: keep
- [x] `apps/web/src/lib/api/client.test.ts` — verdict: keep
- [x] `apps/web/src/lib/api/schemas/contract-cases.test.ts` — verdict: keep
- [x] `apps/web/src/lib/api/schemas/events.test.ts` — verdict: keep
- [x] `apps/web/src/lib/api/sse.test.ts` — verdict: keep
- [x] `apps/web/src/lib/queries/apps.test.ts` — verdict: **update** — Majority of the file re-proves `selectInfiniteList` at a non-owner. `selectDeploymentList` is a one-line delegation with a flat `deployment.id` key extractor (apps/web/src/lib/queries/deployments.ts:38-42) to `selectInfiniteList` (apps/web/src/lib/queries/infinite-list.ts:14-36). The 205-item flattening case (apps/web/src/lib/queries/apps.test.ts:8-23) is already proven at the cheaper authoritative owner by apps/web/src/lib/queries/infinite-list.test.ts:6-22, and the dedup half of the second case (apps/web/src/lib/queries/apps.test.ts:32-34) by apps/web/src/lib/queries/infinite-list.test.ts:24-38 with an identical flat-id fixture. Only `nextDeploymentCursor`'s repeated-cursor guard (apps/web/src/lib/queries/deployments.ts:46-52, asserted at apps/web/src/lib/queries/apps.test.ts:35-36) is unique — it stops an unbounded refetch loop against the API, which the app CLAUDE.md forbids — so the file is not a full delete. Note the file is also misnamed: it lives at lib/queries/apps.test.ts but imports and tests ./deployments.
  - **How:** Delete the 205-deployment flattening test (lines 8-23) and the dedup half of the second test (lines 26-34, including the `deployment`/`deploymentPage` fixture bulk they need). Keep only the `nextDeploymentCursor(first, [first]) === "cursor-2"` / `nextDeploymentCursor(repeated, [first, repeated]) === undefined` assertions (lines 35-36) and rename the file to deployments.test.ts so it sits beside its owner. Better still, hoist the identical cursor-repeat guard out of deployments.ts:46 and containers.ts:52 into infinite-list.ts and prove it once there.
- [x] `apps/web/src/lib/queries/containers.test.ts` — verdict: keep
- [x] `apps/web/src/lib/queries/infinite-list.test.ts` — verdict: keep
- [x] `apps/web/src/lib/queries/shells.test.ts` — verdict: **update** — Vacuous setup and a tautological assertion. Line 7 seeds `localStorage.setItem("lazycloud.auth.token", "long-lived-secret")`, but production's auth store key is `lazycloud_web_token` (apps/web/src/lib/auth.ts:1), so nothing is ever seeded. More fundamentally `shellWebSocketUrl` (apps/web/src/lib/queries/shells.ts:26-34) is a pure string builder over its three arguments and never reads the token store at all, so `expect(url).not.toContain("long-lived-secret")` (line 12) can never fail under any implementation. The surviving invariant — only the one-use ticket appears in the WebSocket URL — is genuinely worth keeping (browsers cannot send Authorization on upgrade, so a regression would put the long-lived bearer in a URL that lands in proxy/access logs), and `not.toContain("token=")` / `not.toContain("authorization=")` at lines 13-14 do protect it.
  - **How:** Delete the localStorage seeding at line 7 and the `not.toContain("long-lived-secret")` assertion at line 12. Keep the `?ticket=` / `not.toContain("token=")` / `not.toContain("authorization=")` assertions. If the intent was to prove the builder cannot reach the token store, assert it against the real key by seeding `lazycloud_web_token` instead — but that is still unreachable from a pure function, so removal is the honest fix.
- [x] `apps/web/src/lib/queries/usage.test.ts` — verdict: **delete** — Same defect as workspace-recovery plus a duplicated invariant. Test 1 (apps/web/src/lib/queries/usage.test.ts:11-22) asserts only the `meta` constant produced by `workspaceLiveQueryMeta(true)` (apps/web/src/lib/queries/workspace-keys.ts:228-238) at two call sites (apps/web/src/lib/queries/usage.ts:39, :88) — config shape, not behaviour. Test 2 (apps/web/src/lib/queries/usage.test.ts:24-33) asserts the internal query-key tuple and the literal `refetchInterval === 60_000` (apps/web/src/lib/queries/usage.ts:38). The one materially useful invariant it gestures at — the current period is backend-resolved and the browser sends no start/end — is already proven on the production-representative path by the real request assertions at apps/web/tests/e2e/usage-billing.spec.ts:17-22 (`period=current` present, `start`/`end` absent). Fails gates 1, 2 and 3.
- [x] `apps/web/src/lib/queries/workspace-recovery.test.ts` — verdict: **delete** — Pure implementation-shape/constant assertion, no production behaviour executed. The it.each matrix builds 7 query-option objects and asserts only `options.meta` matches `{workspaceLiveEnabled:true, workspaceLiveCritical:true}` and that `refetchInterval` is absent (apps/web/src/lib/queries/workspace-recovery.test.ts:18-24). Every one of those builders sets `meta: workspaceLiveQueryMeta(true)`, and that helper is a literal object return at apps/web/src/lib/queries/workspace-keys.ts:228-238 — so the test restates a constant through 7 call sites. No recovery pass runs; the actual stream-tail recovery lives in the provider (apps/web/src/components/shared/WorkspaceLiveUpdates/index.tsx) and is not exercised here. Fails gate 1 (config shape, not behaviour at a stable owner) and gate 2 (a wrong flag changes refresh timing, not authorization/data integrity/durability/cleanup/public contract). Also exactly the forbidden form: a matrix over an option-builder inventory asserting constants.
- [x] `apps/web/src/routes/w/$workspace/apps/-components/app-activity-buckets.test.ts` — verdict: keep
- [x] `apps/web/src/routes/w/$workspace/apps/-workloads/grouping.test.ts` — verdict: keep
- [x] `apps/web/src/routes/w/$workspace/apps/-workloads/playground-form.test.ts` — verdict: keep
- [x] `apps/web/src/routes/w/$workspace/settings/-components/AwsConnectionDialog/controller.test.ts` — verdict: **update** — The fourth case asserts a mock call transcript in order rather than an outcome: `expect(invalidateQueries.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([awsConnection, policy, instances])` (apps/web/src/routes/w/$workspace/settings/-components/AwsConnectionDialog/controller.test.ts:122-126). Call order of `invalidateQueries` is not observable behaviour — reordering the three lines in the controller changes nothing a user sees but fails the test — and the repo's Do-not-test list names "call order" and "mock transcripts" explicitly. The first three cases in the file (single-flight recovery gating at :67-88, scoped retryable errors at :42-65 and :90-107) are real concurrency/error-scoping invariants against a paid external provider and should stay.
  - **How:** In the fourth test, replace the ordered `invalidateQueries.mock.calls` assertion (lines 111, 122-126) with an outcome assertion: seed the three AWS projections into the QueryClient before removal and assert afterwards that each is invalidated/refetching (e.g. via `queryClient.getQueryState(key)?.isInvalidated`), order-independent. Keep the `removeOpen === false` and `onClose` assertions at lines 120-121.
- [x] `apps/web/src/routes/w/$workspace/settings/-components/ComputePolicyForm/controller.test.ts` — verdict: keep
- [x] `apps/web/src/routes/w/$workspace/settings/-components/WorkspaceIdentity/controller.test.ts` — verdict: keep
- [x] `apps/web/src/routes/w/$workspace/settings/-components/WorkspaceTokens/controller.test.ts` — verdict: keep
- [x] `apps/web/src/routes/w/$workspace/settings/-components/region-selection.test.ts` — verdict: keep
- [x] `apps/web/src/routes/w/$workspace/storage/-components/EncodedValuePreview/decode.test.ts` — verdict: keep
- [x] `apps/web/tests/e2e/marketing.spec.ts` — verdict: **update** — The first test is dominated by literal copy, label inventories, element counts and pixel equality — every category the repo's Do-not-test list names. It pins the exact page title (line 20 vs apps/web/src/routes/index.tsx:6), the exact h1 sentence (lines 25-27 vs apps/web/src/routes/index.lazy.tsx:219), the full six-item hero tab label list (lines 32-40 vs apps/web/src/routes/index.lazy.tsx:104-139), an exact count of three "Private beta" buttons (lines 51-56) against four occurrences in source (apps/web/src/routes/index.lazy.tsx:228, apps/web/src/routes/-marketing/MarketingLayout.tsx:82 and :219, apps/web/src/routes/-marketing/MarketingPrimitives.tsx:222), marketing prose strings (lines 29, 45-47, 57-58), and pixel equality of the surface to the viewport (lines 66-68). All of these fail gate 2: copy edits break the test without any material user impact. The material outcomes in the file do pass the gate and must stay: the public marketing route issues no /api/ or /auth/ request (lines 7-12, 77), no horizontal overflow (line 66), zero wcag2a/wcag2aa violations (lines 72-76), and the second test's authorization boundary — unauthenticated /dashboard shows the token prompt and issues no /api/v1/workspaces request (lines 81-95).
  - **How:** Delete the literal-copy and inventory assertions: the exact title and meta description (lines 20-24), the exact h1 accessible name (lines 25-27), the CLI/prose string checks (lines 29, 45-47, 57-58), the six-label tab list (lines 32-40), the "Private beta" count (lines 51-56), and the height/width viewport equality (lines 67-68). Keep the `lazycloud-admin` absence check (line 30) only if the rule "the internal admin CLI never appears on a public page" is stated as a real constraint. Retain the no-authenticated-request tracking, the horizontal-overflow check, the axe run, the console-error check, and the whole second test unchanged.
- [x] `apps/web/tests/e2e/onboarding.spec.ts` — verdict: **update** — Two problems. First, the empty-state test asserts seven literal quickstart copy lines (apps/web/tests/e2e/onboarding.spec.ts:77-83: `uv tool install lazycloud`, `$ lazycloud login`, `lazycloud quickstart`, `lazycloud deploy quickstart.py:hello`, `lazycloud run quickstart.py:hello`, `lazycloud task result <task-id>`, `lazycloud task logs <task-id>`) that are pure copy — the test's actual subject is the live flip at lines 88-90, which only needs one stable empty-state anchor. Second, the third test (lines 117-122) asserts a placeholder string is visible and that Continue is disabled with no code entered; that is trivial form state and literal copy, materially affecting nothing, and fails gate 2. Also note `firstAppItem` (lines 7-21) uses stale keys `stub`/`deployment` where the schema now reads `latest_workload`/`latest_deployment` (apps/web/src/lib/api/schemas/apps.ts:93-95); Zod strips them so the test still passes, but the fixture no longer represents a real response. The live-flip case and the device-approval case (lines 93-115) are material browser-only outcomes and should stay.
  - **How:** In the first test keep only `expect(page.getByText("Deploy your first app")).toBeVisible()` (line 76) as the empty-state anchor and the live-flip assertions (lines 88-90); delete lines 77-84. Delete the third test entirely (lines 117-122). Update `firstAppItem` to use `latest_workload`/`latest_deployment` so the fixture matches apps/web/src/lib/api/schemas/apps.ts:93-95.
- [x] `apps/web/tests/e2e/production.spec.ts` — verdict: keep
- [x] `apps/web/tests/e2e/smoke.spec.ts` — verdict: **update** — Several assertions and their supporting route mocks protect a capability gate that production no longer has, so they can never fail. `primaryNav` is a static list of Apps/Tasks/Storage/Usage and `settingsNav` is Settings alone (apps/web/src/components/shared/AppShell/index.tsx:45-57, rendered at :185-203) — there is no admin-conditional Compute entry for any token, and Compute is now a tab inside Settings (apps/web/src/routes/w/$workspace/settings/index.tsx:43). That makes the Compute-hidden assertions at lines 196 and 202 vacuous, together with the comment and the `**/api/v1/workers*` 403 mock at lines 170-173 that exist only to simulate the removed non-admin gate. Likewise `Home` (line 191) appears in no nav definition, and the `/api/v1/concurrency-limits*` fixture at lines 23-43 exists only to feed the `cpu concurrency` absence check at line 207, which nothing renders. The rest of the file — /dashboard landing on /w/acme/apps, workspace switch navigating and persisting, and keyboard search reaching a canonical URL while hiding raw IDs — is real browser behaviour not provable more cheaply.
  - **How:** Delete the vacuous negatives at lines 191, 196, 202 and 207, plus the now-unused `**/api/v1/workers*` mock (lines 170-173) and the `**/api/v1/concurrency-limits*` fixture (lines 23-43). Keep the landing/routing, workspace-switch persistence, and global-search cases. If admin-only surfacing is still a wanted invariant, assert it where the gate actually lives rather than against the shell nav.
- [x] `apps/web/tests/e2e/usage-billing.spec.ts` — verdict: keep
