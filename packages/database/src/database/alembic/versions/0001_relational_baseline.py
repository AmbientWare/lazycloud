"""Initial schema for the owner-authorized database reset.

This revision starts a new history and cannot upgrade the previous installation.
Subsequent deployed changes require forward revisions.
"""

from alembic import op

revision = "0001_relational_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE EXTENSION IF NOT EXISTS pgcrypto
""")
    op.execute("""
CREATE EXTENSION IF NOT EXISTS btree_gist
""")
    op.execute("""
CREATE TABLE billing_compute_rates (
	billing_owner VARCHAR(40) NOT NULL,
	rate_class VARCHAR(64) DEFAULT 'auto' NOT NULL,
	gpu_type VARCHAR(64) NOT NULL,
	pricing_version VARCHAR(64) NOT NULL,
	effective_at TIMESTAMP WITH TIME ZONE NOT NULL,
	valid_until TIMESTAMP WITH TIME ZONE,
	nanos_per_container_second NUMERIC(30, 12) NOT NULL,
	nanos_per_cpu_core_second NUMERIC(30, 12) NOT NULL,
	nanos_per_memory_gib_second NUMERIC(30, 12) NOT NULL,
	nanos_per_gpu_card_second NUMERIC(30, 12) NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ex_billing_compute_rates_window EXCLUDE USING gist (billing_owner WITH =, rate_class WITH =, gpu_type WITH =, tstzrange(effective_at, valid_until, '[)') WITH &&),
	CONSTRAINT uq_billing_compute_rates_start UNIQUE (billing_owner, rate_class, gpu_type, effective_at),
	CONSTRAINT ck_billing_compute_rates_owner CHECK (billing_owner IN ('platform_fleet', 'connected_cloud', 'self_hosted')),
	CONSTRAINT ck_billing_compute_rates_window CHECK (valid_until IS NULL OR valid_until > effective_at),
	CONSTRAINT ck_billing_compute_rates_nonnegative CHECK (nanos_per_container_second >= 0 AND nanos_per_cpu_core_second >= 0 AND nanos_per_memory_gib_second >= 0 AND nanos_per_gpu_card_second >= 0)
)
""")
    op.execute("""
CREATE INDEX ix_billing_compute_rates_lookup ON billing_compute_rates (billing_owner, rate_class, gpu_type, effective_at)
""")
    op.execute("""
CREATE TABLE billing_platform_rates (
	pricing_version VARCHAR(64) NOT NULL,
	effective_at TIMESTAMP WITH TIME ZONE NOT NULL,
	valid_until TIMESTAMP WITH TIME ZONE,
	nanos_per_egress_byte NUMERIC(30, 12) NOT NULL,
	nanos_per_volume_byte_second NUMERIC(30, 12) NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ex_billing_platform_rates_window EXCLUDE USING gist (tstzrange(effective_at, valid_until, '[)') WITH &&),
	CONSTRAINT uq_billing_platform_rates_start UNIQUE (effective_at),
	CONSTRAINT ck_billing_platform_rates_window CHECK (valid_until IS NULL OR valid_until > effective_at),
	CONSTRAINT ck_billing_platform_rates_nonnegative CHECK (nanos_per_egress_byte >= 0 AND nanos_per_volume_byte_second >= 0)
)
""")
    op.execute("""
CREATE INDEX ix_billing_platform_rates_lookup ON billing_platform_rates (effective_at)
""")
    op.execute("""
CREATE TABLE billing_webhook_events (
	event_id VARCHAR(255) NOT NULL,
	event_type VARCHAR(128) NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (event_id)
)
""")
    op.execute("""
CREATE TABLE aws_authorization_cleanup_tombstones (
	user_id UUID NOT NULL,
	connection_id UUID NOT NULL,
	account_id VARCHAR(12) NOT NULL,
	status VARCHAR(32) NOT NULL,
	provider_operation_id VARCHAR(128) NOT NULL,
	revision BIGINT NOT NULL,
	next_reconcile_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	claim_token VARCHAR(36),
	claim_expires_at TIMESTAMP WITH TIME ZONE,
	reconcile_attempt_count BIGINT NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	payload JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_aws_authorization_cleanup_operation UNIQUE (provider_operation_id),
	CONSTRAINT ck_aws_authorization_cleanup_revision CHECK (revision > 0),
	CONSTRAINT ck_aws_authorization_cleanup_attempts CHECK (reconcile_attempt_count >= 0)
)
""")
    op.execute("""
CREATE INDEX ix_aws_authorization_cleanup_due ON aws_authorization_cleanup_tombstones (next_reconcile_at, claim_expires_at)
""")
    op.execute("""
CREATE TABLE email_outbox (
	recipient VARCHAR(320) NOT NULL,
	subject VARCHAR(998) NOT NULL,
	html_body TEXT NOT NULL,
	text_body TEXT NOT NULL,
	status VARCHAR(32) NOT NULL,
	attempts INTEGER NOT NULL,
	claim_token VARCHAR(64),
	claimed_at TIMESTAMP WITH TIME ZONE,
	next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
	sent_at TIMESTAMP WITH TIME ZONE,
	last_error VARCHAR(500) NOT NULL,
	provider_message_id VARCHAR(128) NOT NULL,
	delivery_state VARCHAR(32) NOT NULL,
	delivery_event_at TIMESTAMP WITH TIME ZONE,
	delivery_detail VARCHAR(500) NOT NULL,
	redacted_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_email_outbox_status CHECK (status IN ('pending', 'sending', 'sent', 'abandoned')),
	CONSTRAINT ck_email_outbox_attempts CHECK (attempts >= 0),
	CONSTRAINT ck_email_outbox_claim CHECK ((status = 'sending') = (claim_token IS NOT NULL)),
	CONSTRAINT ck_email_outbox_delivery_state CHECK (delivery_state IN ('queued', 'sent', 'delivered', 'bounced', 'complained', 'failed'))
)
""")
    op.execute("""
CREATE INDEX ix_email_outbox_ready ON email_outbox (next_attempt_at, created_at) WHERE status = 'pending'
""")
    op.execute("""
CREATE INDEX ix_email_outbox_stuck ON email_outbox (claimed_at) WHERE status = 'sending'
""")
    op.execute("""
CREATE UNIQUE INDEX uq_email_outbox_provider_message ON email_outbox (provider_message_id) WHERE provider_message_id <> ''
""")
    op.execute("""
CREATE TABLE identity_bootstrap_claims (
	claim_key VARCHAR(64) NOT NULL,
	request_id VARCHAR(128) NOT NULL,
	admin_token_id UUID,
	published_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (claim_key)
)
""")
    op.execute("""
CREATE TABLE identity_admin_recovery_requests (
	request_id VARCHAR(128) NOT NULL,
	workspace_id UUID,
	admin_token_id UUID,
	published_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (request_id)
)
""")
    op.execute("""
CREATE TABLE users (
	display_name VARCHAR(255) NOT NULL,
	email VARCHAR(320) NOT NULL,
	avatar_url VARCHAR(1024) NOT NULL,
	role VARCHAR(32) NOT NULL,
	status VARCHAR(32) NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_users_role CHECK (role IN ('administrator', 'member')),
	CONSTRAINT ck_users_status CHECK (status IN ('active', 'disabled'))
)
""")
    op.execute("""
CREATE INDEX ix_users_email ON users (email)
""")
    op.execute("""
CREATE TABLE workspaces (
	external_id UUID DEFAULT gen_random_uuid() NOT NULL,
	name VARCHAR(240) NOT NULL,
	status VARCHAR(32) NOT NULL,
	signing_key VARCHAR(512),
	signing_key_prefix VARCHAR(120),
	primary_token_id UUID,
	concurrency_limit_id UUID,
	storage_backend VARCHAR(80) NOT NULL,
	storage_bucket VARCHAR(255),
	storage_prefix TEXT NOT NULL,
	storage_endpoint_url TEXT,
	storage_region VARCHAR(128),
	storage_access_key TEXT,
	storage_secret_key TEXT,
	storage_force_path_style BOOLEAN,
	labels JSONB NOT NULL,
	metadata JSONB NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_workspaces_external_id UNIQUE (external_id),
	CONSTRAINT ck_workspaces_status CHECK (status IN ('active', 'disabled', 'deleting', 'deleted'))
)
""")
    op.execute("""
CREATE INDEX ix_workspaces_external_id ON workspaces (external_id)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_workspaces_name ON workspaces (name) WHERE status <> 'deleted'
""")
    op.execute("""
CREATE TABLE image_archives (
	image_id VARCHAR(512) NOT NULL,
	bucket VARCHAR(255) NOT NULL,
	object_key TEXT NOT NULL,
	size_bytes BIGINT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	registry_ref TEXT NOT NULL,
	manifest_digest VARCHAR(71) NOT NULL,
	architecture VARCHAR(16) NOT NULL,
	format_version INTEGER NOT NULL,
	cleanup_claimed_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_image_archives_image_id UNIQUE (image_id),
	CONSTRAINT ck_image_archives_size_positive CHECK (size_bytes > 0),
	CONSTRAINT ck_image_archives_sha256_complete CHECK (length(sha256) = 64),
	CONSTRAINT ck_image_archives_object_key_present CHECK (object_key <> ''),
	CONSTRAINT ck_image_archives_format_version CHECK (format_version >= 1),
	CONSTRAINT ck_image_archives_architecture CHECK (architecture IN ('', 'amd64', 'arm64'))
)
""")
    op.execute("""
CREATE INDEX ix_image_archives_cleanup_claimed_at ON image_archives (cleanup_claimed_at)
""")
    op.execute("""
CREATE INDEX ix_image_archives_updated_at ON image_archives (updated_at)
""")
    op.execute("""
CREATE TABLE worker_events (
	event_data JSONB NOT NULL,
	worker_id VARCHAR(160) NOT NULL,
	event_type VARCHAR(160) NOT NULL,
	resource_id VARCHAR(160),
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id)
)
""")
    op.execute("""
CREATE INDEX ix_worker_events_created ON worker_events (created_at)
""")
    op.execute("""
CREATE INDEX ix_worker_events_resource ON worker_events (resource_id)
""")
    op.execute("""
CREATE INDEX ix_worker_events_type_created ON worker_events (event_type, created_at)
""")
    op.execute("""
CREATE INDEX ix_worker_events_worker_created ON worker_events (worker_id, created_at)
""")
    op.execute("""
CREATE TABLE cache_entries (
	key VARCHAR(512) NOT NULL,
	path TEXT NOT NULL,
	size BIGINT NOT NULL,
	sha256 VARCHAR(128) NOT NULL,
	hits BIGINT NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_cache_entries_key UNIQUE (key),
	CONSTRAINT ck_cache_entries_size_nonnegative CHECK (size >= 0),
	CONSTRAINT ck_cache_entries_hits_nonnegative CHECK (hits >= 0),
	CONSTRAINT ck_cache_entries_timestamp_order CHECK (created_at <= updated_at)
)
""")
    op.execute("""
CREATE TABLE apps (
	metadata JSONB NOT NULL,
	workspace_id UUID NOT NULL,
	stub_id UUID,
	name VARCHAR(240) NOT NULL,
	version INTEGER NOT NULL,
	public BOOLEAN NOT NULL,
	lifecycle_state VARCHAR(32) NOT NULL,
	lifecycle_revision BIGINT NOT NULL,
	lifecycle_target VARCHAR(16),
	lifecycle_operation_id UUID,
	lifecycle_failure VARCHAR(500),
	reconcile_claim_id UUID,
	reconcile_claimed_at TIMESTAMP WITH TIME ZONE,
	reconcile_attempt_count BIGINT NOT NULL,
	lifecycle_event_id UUID,
	lifecycle_event_created_at TIMESTAMP WITH TIME ZONE,
	lifecycle_change_published_at TIMESTAMP WITH TIME ZONE,
	deleted_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_apps_lifecycle_revision CHECK (lifecycle_revision >= 0),
	CONSTRAINT ck_apps_reconcile_attempt_count CHECK (reconcile_attempt_count >= 0),
	CONSTRAINT ck_apps_deleted_state_timestamp CHECK ((lifecycle_state = 'deleted') = (deleted_at IS NOT NULL)),
	CONSTRAINT ck_apps_lifecycle_state CHECK (lifecycle_state IN ('active', 'paused', 'deleted', 'pausing', 'resuming', 'deleting', 'cleanup_failed')),
	CONSTRAINT ck_apps_lifecycle_target CHECK (lifecycle_target IS NULL OR lifecycle_target IN ('active', 'paused', 'deleted')),
	CONSTRAINT ck_apps_unfinished_operation CHECK ((lifecycle_state IN ('pausing', 'resuming', 'deleting', 'cleanup_failed')) = (lifecycle_target IS NOT NULL AND lifecycle_operation_id IS NOT NULL)),
	CONSTRAINT ck_apps_lifecycle_transition CHECK ((lifecycle_state <> 'pausing' OR lifecycle_target = 'paused') AND (lifecycle_state <> 'resuming' OR lifecycle_target = 'active') AND (lifecycle_state <> 'deleting' OR lifecycle_target = 'deleted')),
	CONSTRAINT ck_apps_lifecycle_publication CHECK ((lifecycle_event_id IS NULL) = (lifecycle_event_created_at IS NULL) AND (lifecycle_change_published_at IS NULL OR lifecycle_event_id IS NOT NULL)),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_apps_lifecycle_reconcile ON apps (lifecycle_state, reconcile_claimed_at, updated_at)
""")
    op.execute("""
CREATE INDEX ix_apps_name ON apps (name)
""")
    op.execute("""
CREATE INDEX ix_apps_workspace_updated ON apps (workspace_id, updated_at, id)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_apps_workspace_name_active ON apps (workspace_id, name) WHERE deleted_at IS NULL
""")
    op.execute("""
CREATE TABLE billing_accounts (
	user_id UUID NOT NULL,
	status VARCHAR(32) NOT NULL,
	provider_customer_id VARCHAR(255) NOT NULL,
	provider_subscription_id VARCHAR(255) NOT NULL,
	plan VARCHAR(32) NOT NULL,
	subscription_terms_version VARCHAR(32),
	scheduled_terms_version VARCHAR(32),
	scheduled_change_at TIMESTAMP WITH TIME ZONE,
	payment_method_attached_at TIMESTAMP WITH TIME ZONE,
	complimentary_since TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_billing_accounts_user UNIQUE (user_id),
	CONSTRAINT ck_billing_accounts_status CHECK (status IN ('active', 'past_due')),
	CONSTRAINT ck_billing_accounts_plan CHECK (plan IN ('', 'free', 'team', 'business')),
	CONSTRAINT ck_billing_accounts_subscription_terms CHECK (subscription_terms_version IS NULL OR (plan = 'free' AND subscription_terms_version IN ('free-v1', 'free-v2')) OR (plan = 'team' AND subscription_terms_version IN ('team-v1', 'team-v2', 'team-v3')) OR (plan = 'business' AND subscription_terms_version IN ('business-v1', 'business-v2'))),
	CONSTRAINT ck_billing_accounts_scheduled_terms CHECK ((scheduled_terms_version IS NULL AND scheduled_change_at IS NULL) OR (scheduled_terms_version IS NOT NULL AND scheduled_terms_version IN ('free-v1', 'team-v1', 'free-v2', 'team-v2', 'business-v1', 'team-v3', 'business-v2') AND scheduled_change_at IS NOT NULL)),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_billing_accounts_provider_customer ON billing_accounts (provider_customer_id) WHERE provider_customer_id <> ''
""")
    op.execute("""
CREATE UNIQUE INDEX uq_billing_accounts_provider_subscription ON billing_accounts (provider_subscription_id) WHERE provider_subscription_id <> ''
""")
    op.execute("""
CREATE TABLE billing_allowance_periods (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	user_id UUID NOT NULL,
	period_started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	period_ended_at TIMESTAMP WITH TIME ZONE NOT NULL,
	allowance_nanos BIGINT NOT NULL,
	spent_nanos BIGINT NOT NULL,
	credit_confirmed_at TIMESTAMP WITH TIME ZONE,
	funded_terms_version VARCHAR(32),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_billing_allowance_periods_period UNIQUE (user_id, period_started_at),
	CONSTRAINT ck_billing_allowance_periods_window CHECK (period_ended_at > period_started_at),
	CONSTRAINT ck_billing_allowance_periods_nonnegative CHECK (spent_nanos >= 0 AND allowance_nanos >= 0),
	CONSTRAINT ck_billing_allowance_periods_funded_terms CHECK (funded_terms_version IS NULL OR funded_terms_version IN ('free-v1', 'team-v1', 'free-v2', 'team-v2', 'business-v1', 'team-v3', 'business-v2')),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_billing_allowance_periods_lookup ON billing_allowance_periods (user_id, period_started_at, period_ended_at)
""")
    op.execute("""
CREATE TABLE billing_credit_lots (
	id UUID NOT NULL,
	user_id UUID NOT NULL,
	source_id VARCHAR(255) NOT NULL,
	kind VARCHAR(32) NOT NULL,
	amount_nanos BIGINT NOT NULL,
	effective_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_billing_credit_lots_source UNIQUE (user_id, source_id),
	CONSTRAINT ck_billing_credit_lots_amount CHECK (amount_nanos > 0),
	CONSTRAINT ck_billing_credit_lots_kind CHECK (kind IN ('purchased', 'subscription', 'trial')),
	CONSTRAINT ck_billing_credit_lots_expiry CHECK (expires_at IS NULL OR expires_at > effective_at),
	CONSTRAINT ck_billing_credit_lots_purchased CHECK (kind <> 'purchased' OR expires_at IS NULL),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_billing_credit_lots_account ON billing_credit_lots (user_id, effective_at, expires_at)
""")
    op.execute("""
CREATE TABLE billing_credit_settlements (
	usage_record_id UUID NOT NULL,
	user_id UUID NOT NULL,
	gross_nanos BIGINT NOT NULL,
	credited_nanos BIGINT,
	payable_nanos BIGINT,
	waived_nanos BIGINT DEFAULT '0' NOT NULL,
	settled_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (usage_record_id),
	CONSTRAINT ck_billing_credit_settlements_gross CHECK (gross_nanos >= 0),
	CONSTRAINT ck_billing_credit_settlements_amounts CHECK ((settled_at IS NULL AND credited_nanos IS NULL AND payable_nanos IS NULL) OR (settled_at IS NOT NULL AND credited_nanos IS NOT NULL AND payable_nanos IS NOT NULL AND credited_nanos >= 0 AND payable_nanos >= 0 AND waived_nanos >= 0 AND credited_nanos + payable_nanos + waived_nanos = gross_nanos)),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_billing_credit_settlements_pending ON billing_credit_settlements (user_id, settled_at)
""")
    op.execute("""
CREATE TABLE container_billing_shapes (
	container_id UUID NOT NULL,
	workspace_id UUID NOT NULL,
	billing_owner VARCHAR(40) NOT NULL,
	rate_class VARCHAR(64) DEFAULT 'auto' NOT NULL,
	gpu_type VARCHAR(64) NOT NULL,
	cpu_millicores INTEGER NOT NULL,
	memory_mib INTEGER NOT NULL,
	gpu_count INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (container_id),
	CONSTRAINT ck_container_billing_shapes_owner CHECK (billing_owner IN ('platform_fleet', 'connected_cloud', 'self_hosted')),
	CONSTRAINT ck_container_billing_shapes_nonnegative CHECK (cpu_millicores >= 0 AND memory_mib >= 0 AND gpu_count >= 0),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE TABLE billing_meter_outbox (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	workspace_id UUID NOT NULL,
	identifier VARCHAR(255) NOT NULL,
	usage_record_id UUID NOT NULL,
	provider_customer_id VARCHAR(255) NOT NULL,
	meter_event_name VARCHAR(128) NOT NULL,
	value_nanos BIGINT NOT NULL,
	pricing_version VARCHAR(512) NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	metering_ended_at TIMESTAMP WITH TIME ZONE NOT NULL,
	status VARCHAR(16) NOT NULL,
	attempts INTEGER NOT NULL,
	next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
	claimed_at TIMESTAMP WITH TIME ZONE,
	claim_token UUID,
	last_error VARCHAR(512) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_billing_meter_outbox_identifier UNIQUE (identifier),
	CONSTRAINT ck_billing_meter_outbox_status CHECK (status IN ('pending', 'sending', 'sent', 'abandoned', 'waived')),
	CONSTRAINT ck_billing_meter_outbox_value CHECK (value_nanos >= 0),
	CONSTRAINT ck_billing_meter_outbox_window CHECK (metering_ended_at > occurred_at),
	CONSTRAINT ck_billing_meter_outbox_attempts CHECK (attempts >= 0),
	CONSTRAINT ck_billing_meter_outbox_claim CHECK ((status = 'sending') = (claim_token IS NOT NULL)),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_billing_meter_outbox_customer_window ON billing_meter_outbox (provider_customer_id, occurred_at)
""")
    op.execute("""
CREATE INDEX ix_billing_meter_outbox_ready ON billing_meter_outbox (next_attempt_at, created_at) WHERE status = 'pending'
""")
    op.execute("""
CREATE INDEX ix_billing_meter_outbox_settled ON billing_meter_outbox (status, updated_at)
""")
    op.execute("""
CREATE INDEX ix_billing_meter_outbox_stuck ON billing_meter_outbox (claimed_at) WHERE status = 'sending'
""")
    op.execute("""
CREATE INDEX ix_billing_meter_outbox_usage ON billing_meter_outbox (usage_record_id)
""")
    op.execute("""
CREATE TABLE billing_plan_change_intents (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	user_id UUID NOT NULL,
	provider_customer_id VARCHAR(255) NOT NULL,
	provider_subscription_id VARCHAR(255) NOT NULL,
	target_plan VARCHAR(32) NOT NULL,
	target_terms_version VARCHAR(32) NOT NULL,
	status VARCHAR(16) NOT NULL,
	attempts INTEGER NOT NULL,
	next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
	claimed_at TIMESTAMP WITH TIME ZONE,
	claim_token UUID,
	last_error VARCHAR(512) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_billing_plan_change_intents_status CHECK (status IN ('pending', 'settling', 'applied', 'not_applied', 'abandoned')),
	CONSTRAINT ck_billing_plan_change_intents_attempts CHECK (attempts >= 0),
	CONSTRAINT ck_billing_plan_change_intents_terms CHECK ((target_plan = 'free' AND target_terms_version IN ('free-v1', 'free-v2')) OR (target_plan = 'team' AND target_terms_version IN ('team-v1', 'team-v2', 'team-v3')) OR (target_plan = 'business' AND target_terms_version IN ('business-v1', 'business-v2'))),
	CONSTRAINT ck_billing_plan_change_intents_claim CHECK ((status = 'settling') = (claim_token IS NOT NULL)),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_billing_plan_change_intents_ready ON billing_plan_change_intents (next_attempt_at, created_at) WHERE status = 'pending'
""")
    op.execute("""
CREATE INDEX ix_billing_plan_change_intents_stuck ON billing_plan_change_intents (claimed_at) WHERE status = 'settling'
""")
    op.execute("""
CREATE UNIQUE INDEX uq_billing_plan_change_intents_open ON billing_plan_change_intents (user_id) WHERE status IN ('pending', 'settling')
""")
    op.execute("""
CREATE TABLE workspace_compute_policies (
	workspace_id UUID NOT NULL,
	revision BIGINT NOT NULL,
	default_pool VARCHAR(240) NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_workspace_compute_policies_workspace UNIQUE (workspace_id),
	CONSTRAINT ck_workspace_compute_policies_revision CHECK (revision > 0),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE TABLE aws_account_connections (
	user_id UUID NOT NULL,
	account_id VARCHAR(12) NOT NULL,
	external_id VARCHAR(256) NOT NULL,
	pool VARCHAR(240) NOT NULL,
	phase VARCHAR(32) NOT NULL,
	revision BIGINT NOT NULL,
	next_reconcile_at TIMESTAMP WITH TIME ZONE,
	claim_token VARCHAR(36),
	claim_expires_at TIMESTAMP WITH TIME ZONE,
	reconcile_attempt_count BIGINT NOT NULL,
	provider_operation_id VARCHAR(128),
	provider_operation_started_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	payload JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_aws_account_connections_user UNIQUE (user_id),
	CONSTRAINT uq_aws_account_connections_external_id UNIQUE (external_id),
	CONSTRAINT ck_aws_account_connections_revision CHECK (revision > 0),
	CONSTRAINT ck_aws_account_connections_reconcile_attempts CHECK (reconcile_attempt_count >= 0),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_aws_account_connections_reconcile_due ON aws_account_connections (next_reconcile_at, claim_expires_at)
""")
    op.execute("""
CREATE TABLE custom_domains (
	user_id UUID NOT NULL,
	hostname VARCHAR(253) NOT NULL,
	phase VARCHAR(32) NOT NULL,
	provider_hostname_id VARCHAR(128),
	required_records JSONB NOT NULL,
	error_code VARCHAR(64),
	error_message VARCHAR(512),
	verified_at TIMESTAMP WITH TIME ZONE,
	last_checked_at TIMESTAMP WITH TIME ZONE,
	deleted_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_custom_domains_phase CHECK (phase IN ('awaiting_verification', 'validating', 'ready', 'action_required')),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_custom_domains_reconcile_due ON custom_domains (phase, last_checked_at)
""")
    op.execute("""
CREATE INDEX ix_custom_domains_user ON custom_domains (user_id, hostname)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_custom_domains_hostname_active ON custom_domains (hostname) WHERE deleted_at IS NULL
""")
    op.execute("""
CREATE TABLE events (
	data JSONB NOT NULL,
	container_id VARCHAR(160),
	workspace_id UUID,
	action VARCHAR(160) NOT NULL,
	level VARCHAR(40) NOT NULL,
	resource_type VARCHAR(120) NOT NULL,
	resource_id VARCHAR(160) NOT NULL,
	message TEXT NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_events_action_created ON events (action, created_at)
""")
    op.execute("""
CREATE INDEX ix_events_container_created ON events (container_id, created_at, id)
""")
    op.execute("""
CREATE INDEX ix_events_created ON events (created_at)
""")
    op.execute("""
CREATE INDEX ix_events_resource ON events (resource_type, resource_id, created_at)
""")
    op.execute("""
CREATE INDEX ix_events_workspace_created ON events (workspace_id, created_at)
""")
    op.execute("""
CREATE TABLE user_identities (
	user_id UUID NOT NULL,
	provider VARCHAR(32) NOT NULL,
	subject VARCHAR(64) NOT NULL,
	subject_login VARCHAR(120) NOT NULL,
	provider_account_created_at TIMESTAMP WITH TIME ZONE,
	last_authenticated_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_user_identities_provider_subject UNIQUE (provider, subject),
	CONSTRAINT uq_user_identities_provider_user UNIQUE (provider, user_id),
	CONSTRAINT ck_user_identities_provider CHECK (provider IN ('github')),
	CONSTRAINT ck_user_identities_subject_present CHECK (subject <> ''),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE TABLE workspace_members (
	workspace_id UUID NOT NULL,
	user_id UUID NOT NULL,
	role VARCHAR(32) NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_workspace_members_workspace_user UNIQUE (workspace_id, user_id),
	CONSTRAINT ck_workspace_members_role CHECK (role IN ('owner', 'administrator', 'member')),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_workspace_members_user ON workspace_members (user_id)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_workspace_members_owner ON workspace_members (workspace_id) WHERE role = 'owner'
""")
    op.execute("""
CREATE TABLE workspace_invitations (
	workspace_id UUID NOT NULL,
	email VARCHAR(320) NOT NULL,
	role VARCHAR(32) NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	invited_by_user_id UUID,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	message_id UUID,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_workspace_invitations_workspace_email UNIQUE (workspace_id, email),
	CONSTRAINT uq_workspace_invitations_token UNIQUE (token_hash),
	CONSTRAINT ck_workspace_invitations_role CHECK (role IN ('administrator', 'member')),
	CONSTRAINT ck_workspace_invitations_email_folded CHECK (email = lower(email)),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(invited_by_user_id) REFERENCES users (id) ON DELETE SET NULL,
	FOREIGN KEY(message_id) REFERENCES email_outbox (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_workspace_invitations_workspace ON workspace_invitations (workspace_id)
""")
    op.execute("""
CREATE TABLE tokens (
	name VARCHAR(240) NOT NULL,
	token_hash VARCHAR(255) NOT NULL,
	prefix VARCHAR(64) NOT NULL,
	kind VARCHAR(64) NOT NULL,
	device_login BOOLEAN DEFAULT false NOT NULL,
	user_id UUID,
	workspace_id UUID,
	worker_id VARCHAR(160) NOT NULL,
	status VARCHAR(64) NOT NULL,
	scopes JSONB NOT NULL,
	reusable BOOLEAN NOT NULL,
	disabled_by_admin BOOLEAN NOT NULL,
	last_used_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE,
	revoked_at TIMESTAMP WITH TIME ZONE,
	consumed_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_tokens_token_hash UNIQUE (token_hash),
	CONSTRAINT ck_tokens_single_principal CHECK ((user_id IS NULL) <> (workspace_id IS NULL)),
	CONSTRAINT ck_tokens_consumed_non_reusable CHECK (consumed_at IS NULL OR reusable = false),
	CONSTRAINT ck_tokens_consumed_terminal CHECK (consumed_at IS NULL OR (status = 'revoked' AND revoked_at IS NOT NULL)),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_tokens_prefix ON tokens (prefix)
""")
    op.execute("""
CREATE INDEX ix_tokens_user_created ON tokens (user_id, created_at, id)
""")
    op.execute("""
CREATE INDEX ix_tokens_workspace_created ON tokens (workspace_id, created_at, id)
""")
    op.execute("""
CREATE TABLE device_authorizations (
	device_code_hash VARCHAR(128) NOT NULL,
	user_code VARCHAR(32) NOT NULL,
	client_name VARCHAR(120) NOT NULL,
	status VARCHAR(32) NOT NULL,
	user_id UUID,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	consumed_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_device_authorizations_device_code_hash UNIQUE (device_code_hash),
	CONSTRAINT uq_device_authorizations_user_code UNIQUE (user_code),
	CONSTRAINT ck_device_authorizations_status CHECK (status IN ('pending', 'approved', 'denied')),
	CONSTRAINT ck_device_authorizations_user_state CHECK ((status = 'approved' AND user_id IS NOT NULL) OR (status IN ('pending', 'denied') AND user_id IS NULL)),
	CONSTRAINT ck_device_authorizations_consumed_terminal CHECK (consumed_at IS NULL OR status IN ('approved', 'denied')),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_device_authorizations_expires_at ON device_authorizations (expires_at)
""")
    op.execute("""
CREATE TABLE concurrency_limits (
	workspace_id UUID NOT NULL,
	name VARCHAR(240) NOT NULL,
	"limit" INTEGER NOT NULL,
	in_flight INTEGER NOT NULL,
	resource_type VARCHAR(120) NOT NULL,
	resource_id VARCHAR(160),
	metadata JSONB NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_concurrency_limits_limit_positive CHECK ("limit" > 0),
	CONSTRAINT ck_concurrency_limits_in_flight_nonnegative CHECK (in_flight >= 0),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_concurrency_limits_workspace_name_created ON concurrency_limits (workspace_id, name, created_at)
""")
    op.execute("""
CREATE TABLE workspace_secrets (
	workspace_id UUID NOT NULL,
	name VARCHAR(240) NOT NULL,
	ciphertext TEXT NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_workspace_secrets_workspace_name UNIQUE (workspace_id, name),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_workspace_secrets_workspace ON workspace_secrets (workspace_id)
""")
    op.execute("""
CREATE TABLE images (
	workspace_id UUID NOT NULL,
	image_id VARCHAR(512) NOT NULL,
	clip_version INTEGER NOT NULL,
	aliases TEXT[] NOT NULL,
	cleanup_claimed_at TIMESTAMP WITH TIME ZONE,
	cleanup_completed_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_images_workspace_image_id UNIQUE (workspace_id, image_id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_images_cleanup_claimed_at ON images (cleanup_claimed_at)
""")
    op.execute("""
CREATE INDEX ix_images_cleanup_completed_at ON images (cleanup_completed_at)
""")
    op.execute("""
CREATE INDEX ix_images_image_id ON images (image_id)
""")
    op.execute("""
CREATE INDEX ix_images_workspace ON images (workspace_id)
""")
    op.execute("""
CREATE TABLE image_builds (
	workspace_id UUID,
	image_id VARCHAR(512),
	cache_key VARCHAR(512),
	archive_path_value TEXT NOT NULL,
	archive_path_digest VARCHAR(64) NOT NULL,
	manifest_path_value TEXT NOT NULL,
	manifest_path_digest VARCHAR(64) NOT NULL,
	dockerfile_path_value TEXT NOT NULL,
	dockerfile_path_digest VARCHAR(64) NOT NULL,
	cache_manifest_path_value TEXT NOT NULL,
	cache_manifest_path_digest VARCHAR(64) NOT NULL,
	cache_publish_key VARCHAR(512) NOT NULL,
	fingerprint VARCHAR(512) NOT NULL,
	image_definition JSONB NOT NULL,
	context_object_id VARCHAR(160),
	dockerfile TEXT,
	context_digest VARCHAR(512),
	tag TEXT,
	published_ref TEXT,
	cache_details JSONB NOT NULL,
	build_container_required BOOLEAN,
	image_archive_format_version INTEGER,
	image_archive_status VARCHAR(80),
	diagnostic_lines TEXT[] NOT NULL,
	error TEXT,
	status VARCHAR(80) NOT NULL,
	phase VARCHAR(80) NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	cleanup_claimed_at TIMESTAMP WITH TIME ZONE,
	publication_claim_id VARCHAR(64) NOT NULL,
	publication_claimed_at TIMESTAMP WITH TIME ZONE,
	execution_cleanup_after TIMESTAMP WITH TIME ZONE,
	dispatch_payload TEXT,
	dispatch_after TIMESTAMP WITH TIME ZONE,
	dispatch_claim_id VARCHAR(64),
	dispatched_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_image_builds_status CHECK (status IN ('pending', 'running', 'complete', 'failed', 'cancelled', 'timeout')),
	CONSTRAINT ck_image_builds_phase CHECK (phase IN ('verify', 'planning', 'submitted', 'manifest', 'complete', 'failed', 'reused')),
	CONSTRAINT ck_image_builds_archive_format_version CHECK (image_archive_format_version IS NULL OR image_archive_format_version >= 1),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_image_builds_active_updated ON image_builds (updated_at) WHERE status IN ('pending', 'running')
""")
    op.execute("""
CREATE INDEX ix_image_builds_archive_path_digest ON image_builds (archive_path_digest)
""")
    op.execute("""
CREATE INDEX ix_image_builds_cache_key ON image_builds (cache_key)
""")
    op.execute("""
CREATE INDEX ix_image_builds_cache_manifest_path_digest ON image_builds (cache_manifest_path_digest)
""")
    op.execute("""
CREATE INDEX ix_image_builds_cache_publish_key ON image_builds (cache_publish_key)
""")
    op.execute("""
CREATE INDEX ix_image_builds_cleanup_claimed_at ON image_builds (cleanup_claimed_at)
""")
    op.execute("""
CREATE INDEX ix_image_builds_cleanup_due ON image_builds (execution_cleanup_after) WHERE execution_cleanup_after IS NOT NULL
""")
    op.execute("""
CREATE INDEX ix_image_builds_context_object ON image_builds (context_object_id)
""")
    op.execute("""
CREATE INDEX ix_image_builds_dispatch_due ON image_builds (dispatch_after) WHERE dispatch_payload IS NOT NULL AND dispatched_at IS NULL
""")
    op.execute("""
CREATE INDEX ix_image_builds_dockerfile_path_digest ON image_builds (dockerfile_path_digest)
""")
    op.execute("""
CREATE INDEX ix_image_builds_image_id ON image_builds (image_id)
""")
    op.execute("""
CREATE INDEX ix_image_builds_manifest_path_digest ON image_builds (manifest_path_digest)
""")
    op.execute("""
CREATE INDEX ix_image_builds_status_created ON image_builds (status, created_at)
""")
    op.execute("""
CREATE INDEX ix_image_builds_workspace_created ON image_builds (workspace_id, created_at)
""")
    op.execute("""
CREATE INDEX ix_image_builds_workspace_fingerprint_status_created ON image_builds (workspace_id, fingerprint, status, created_at)
""")
    op.execute("""
CREATE INDEX ix_image_builds_workspace_image_created ON image_builds (workspace_id, image_id, created_at)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_image_builds_active_workspace_fingerprint ON image_builds (workspace_id, fingerprint) WHERE status IN ('pending', 'running')
""")
    op.execute("""
CREATE TABLE usage_records (
	unit VARCHAR(40) NOT NULL,
	labels JSONB NOT NULL,
	metadata JSONB NOT NULL,
	metering_started_at TIMESTAMP WITH TIME ZONE,
	metering_ended_at TIMESTAMP WITH TIME ZONE,
	app_id VARCHAR(160),
	stub_id VARCHAR(160),
	deployment_id VARCHAR(160),
	gpu VARCHAR(160),
	container_id VARCHAR(160),
	task_id VARCHAR(160),
	worker_id VARCHAR(160),
	workspace_id UUID NOT NULL,
	resource_type VARCHAR(120) NOT NULL,
	resource_id VARCHAR(160) NOT NULL,
	metric VARCHAR(120) NOT NULL,
	quantity FLOAT NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_usage_records_quantity CHECK (quantity >= 0 AND quantity < 'Infinity'::float8),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_usage_records_workspace_created ON usage_records (workspace_id, created_at DESC, id ASC)
""")
    op.execute("""
CREATE INDEX ix_usage_records_workspace_metric_created ON usage_records (workspace_id, metric, created_at DESC, id ASC)
""")
    op.execute("""
CREATE INDEX ix_usage_records_workspace_resource_created ON usage_records (workspace_id, resource_type, resource_id, created_at DESC, id ASC)
""")
    op.execute("""
CREATE TABLE autoscaler_states (
	source VARCHAR(120) NOT NULL,
	target_kind VARCHAR(80) NOT NULL,
	target_id VARCHAR(160) NOT NULL,
	decision VARCHAR(80) NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	name VARCHAR(240) NOT NULL,
	workspace_id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	payload JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_autoscaler_states_workspace_name UNIQUE (workspace_id, name),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_autoscaler_states_target ON autoscaler_states (target_kind, target_id)
""")
    op.execute("""
CREATE INDEX ix_autoscaler_states_workspace_source ON autoscaler_states (workspace_id, source)
""")
    op.execute("""
CREATE TABLE machines (
	workspace_id UUID,
	pool VARCHAR(240) NOT NULL,
	capacity_owner_id VARCHAR(64) NOT NULL,
	provider VARCHAR(120) NOT NULL,
	status VARCHAR(80) NOT NULL,
	address VARCHAR(512),
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	payload JSONB NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_machines_pool_status ON machines (pool, status)
""")
    op.execute("""
CREATE INDEX ix_machines_provider_status ON machines (provider, status)
""")
    op.execute("""
CREATE INDEX ix_machines_workspace_created ON machines (workspace_id, created_at)
""")
    op.execute("""
CREATE INDEX ix_machines_workspace_owner ON machines (workspace_id, capacity_owner_id)
""")
    op.execute("""
CREATE TABLE agents (
	workspace_id UUID,
	name VARCHAR(240) NOT NULL,
	pool VARCHAR(240) NOT NULL,
	status VARCHAR(80) NOT NULL,
	version VARCHAR(120) NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	payload JSONB NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_agents_pool_status ON agents (pool, status)
""")
    op.execute("""
CREATE INDEX ix_agents_workspace_created ON agents (workspace_id, created_at)
""")
    op.execute("""
CREATE TABLE worker_cache_generations (
	worker_id VARCHAR(240) NOT NULL,
	storage_id VARCHAR(512) NOT NULL,
	workspace_id UUID,
	state VARCHAR(32) NOT NULL,
	session_fence INTEGER NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	retired_at TIMESTAMP WITH TIME ZONE,
	storage_destroyed_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_worker_cache_generations_state CHECK (state IN ('initializing', 'available', 'draining', 'retired')),
	CONSTRAINT ck_worker_cache_generations_session_fence_positive CHECK (session_fence > 0),
	CONSTRAINT ck_worker_cache_generations_worker_nonempty CHECK (length(trim(worker_id)) > 0),
	CONSTRAINT ck_worker_cache_generations_storage_nonempty CHECK (length(trim(storage_id)) > 0),
	CONSTRAINT ck_worker_cache_generations_retirement CHECK ((state = 'retired' AND retired_at IS NOT NULL AND storage_destroyed_at IS NOT NULL) OR (state <> 'retired' AND retired_at IS NULL AND storage_destroyed_at IS NULL)),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_worker_cache_generations_last_seen ON worker_cache_generations (last_seen_at, id)
""")
    op.execute("""
CREATE INDEX ix_worker_cache_generations_worker_state ON worker_cache_generations (worker_id, state)
""")
    op.execute("""
CREATE INDEX ix_worker_cache_generations_workspace_state ON worker_cache_generations (workspace_id, state)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_worker_cache_generations_active_storage ON worker_cache_generations (storage_id) WHERE state <> 'retired'
""")
    op.execute("""
CREATE TABLE objects (
	workspace_id UUID NOT NULL,
	bucket VARCHAR(255) NOT NULL,
	key VARCHAR(1024) NOT NULL,
	path TEXT NOT NULL,
	size BIGINT NOT NULL,
	sha256 VARCHAR(128) NOT NULL,
	content_type VARCHAR(255) NOT NULL,
	write_claim_id VARCHAR(64) NOT NULL,
	write_claimed_at TIMESTAMP WITH TIME ZONE,
	cleanup_kind VARCHAR(80) NOT NULL,
	cleanup_claimed_at TIMESTAMP WITH TIME ZONE,
	metadata JSONB NOT NULL,
	artifact_app_name TEXT NOT NULL,
	artifact_filename TEXT NOT NULL,
	artifact_retention_seconds BIGINT,
	artifact_stored_at TIMESTAMP WITH TIME ZONE,
	artifact_deletion_failed BOOLEAN NOT NULL,
	write_created BOOLEAN NOT NULL,
	write_target_path TEXT,
	write_target_size BIGINT,
	write_target_sha256 VARCHAR(128),
	write_target_content_type VARCHAR(255),
	write_target_metadata JSONB,
	write_target_artifact_task_id VARCHAR(64),
	write_target_artifact_app_id VARCHAR(64),
	write_target_artifact_app_name TEXT,
	write_target_artifact_filename TEXT,
	write_target_artifact_retention_seconds BIGINT,
	write_target_artifact_stored_at TIMESTAMP WITH TIME ZONE,
	write_target_artifact_expires_at TIMESTAMP WITH TIME ZONE,
	write_target_artifact_metered_at TIMESTAMP WITH TIME ZONE,
	write_target_artifact_deletion_failed BOOLEAN,
	artifact_task_id VARCHAR(64),
	artifact_app_id VARCHAR(64),
	artifact_expires_at TIMESTAMP WITH TIME ZONE,
	artifact_metered_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_objects_workspace_bucket_key UNIQUE (workspace_id, bucket, key),
	CONSTRAINT ck_objects_size_nonnegative CHECK (size >= 0),
	CONSTRAINT ck_objects_write_claim_target CHECK ((write_claimed_at IS NULL AND write_claim_id = '' AND NOT write_created AND write_target_size IS NULL) OR (write_claimed_at IS NOT NULL AND write_claim_id <> '' AND write_target_size IS NOT NULL AND write_target_size >= 0 AND write_target_path IS NOT NULL AND write_target_sha256 IS NOT NULL AND write_target_content_type IS NOT NULL)),
	CONSTRAINT ck_objects_artifact_retention_positive CHECK (artifact_retention_seconds IS NULL OR artifact_retention_seconds > 0),
	CONSTRAINT ck_objects_artifact_retention_required CHECK (artifact_task_id IS NULL OR artifact_retention_seconds IS NOT NULL),
	CONSTRAINT ck_objects_write_target_artifact_retention CHECK (write_target_artifact_task_id IS NULL OR (write_target_artifact_retention_seconds IS NOT NULL AND write_target_artifact_retention_seconds > 0)),
	CONSTRAINT ck_objects_artifact_expiration_required CHECK (artifact_task_id IS NULL OR write_claimed_at IS NOT NULL OR artifact_expires_at IS NOT NULL),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_objects_artifact_app ON objects (workspace_id, artifact_app_id)
""")
    op.execute("""
CREATE INDEX ix_objects_artifact_expiration ON objects (artifact_expires_at, id)
""")
    op.execute("""
CREATE INDEX ix_objects_artifact_listing ON objects (workspace_id, artifact_task_id, created_at, id)
""")
    op.execute("""
CREATE INDEX ix_objects_artifact_metering ON objects (artifact_metered_at, id)
""")
    op.execute("""
CREATE INDEX ix_objects_cleanup_claimed_at ON objects (cleanup_claimed_at)
""")
    op.execute("""
CREATE INDEX ix_objects_workspace_key ON objects (workspace_id, key)
""")
    op.execute("""
CREATE INDEX ix_objects_workspace_sha256 ON objects (workspace_id, sha256)
""")
    op.execute("""
CREATE INDEX ix_objects_write_claimed_at ON objects (write_claimed_at)
""")
    op.execute("""
CREATE TABLE volumes (
	name VARCHAR(240) NOT NULL,
	workspace_id UUID NOT NULL,
	size_bytes BIGINT NOT NULL,
	deletion_requested_at TIMESTAMP WITH TIME ZONE,
	unfenced_writes_possible BOOLEAN NOT NULL,
	metered_at TIMESTAMP WITH TIME ZONE NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_volumes_workspace_name UNIQUE (workspace_id, name),
	CONSTRAINT ck_volumes_size_bytes_nonnegative CHECK (size_bytes >= 0),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_volumes_deletion_requested_at ON volumes (deletion_requested_at, id)
""")
    op.execute("""
CREATE INDEX ix_volumes_metered_at ON volumes (metered_at, id)
""")
    op.execute("""
CREATE INDEX ix_volumes_workspace ON volumes (workspace_id)
""")
    op.execute("""
CREATE TABLE volume_cleanup (
	volume_id UUID NOT NULL,
	workspace_id UUID NOT NULL,
	swept_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (volume_id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_volume_cleanup_swept_at ON volume_cleanup (swept_at, volume_id)
""")
    op.execute("""
CREATE TABLE storage_access_observations (
	id UUID NOT NULL,
	provider VARCHAR(32) NOT NULL,
	bucket VARCHAR(255) NOT NULL,
	request_id VARCHAR(128) NOT NULL,
	operation VARCHAR(64) NOT NULL,
	request_class VARCHAR(16) NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	status_code INTEGER NOT NULL,
	response_bytes BIGINT,
	source_region VARCHAR(64) NOT NULL,
	transfer_evidence VARCHAR(16) NOT NULL,
	workspace_id UUID,
	PRIMARY KEY (id),
	CONSTRAINT ck_storage_access_bytes CHECK (response_bytes IS NULL OR response_bytes >= 0),
	CONSTRAINT ck_storage_access_status CHECK (status_code BETWEEN 100 AND 599),
	CONSTRAINT ck_storage_access_class CHECK (request_class IN ('read','write','delete','other')),
	CONSTRAINT ck_storage_access_transfer CHECK (transfer_evidence IN ('same_region','other_region','unknown')),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_storage_access_occurred ON storage_access_observations (occurred_at)
""")
    op.execute("""
CREATE INDEX ix_storage_access_workspace_occurred ON storage_access_observations (workspace_id, occurred_at)
""")
    op.execute("""
CREATE TABLE storage_retention_periods (
	user_id UUID NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	ended_at TIMESTAMP WITH TIME ZONE,
	notification_message_id VARCHAR(255) NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_storage_retention_periods_window CHECK (ended_at IS NULL OR ended_at >= started_at),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_storage_retention_periods_account ON storage_retention_periods (user_id, started_at)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_storage_retention_periods_open ON storage_retention_periods (user_id) WHERE ended_at IS NULL
""")
    op.execute("""
CREATE TABLE stubs (
	handler TEXT,
	deployment_id UUID,
	configuration JSONB NOT NULL,
	metadata JSONB NOT NULL,
	image_id VARCHAR(160),
	image_context_object_id UUID,
	copied_object_ids UUID[],
	config_copied_object_ids UUID[],
	autoscaling_enabled BOOLEAN,
	runtime_region VARCHAR(80),
	runtime_availability_zone VARCHAR(160),
	runtime_cpu JSONB,
	runtime_cpu_millicores INTEGER,
	runtime_memory JSONB,
	runtime_disk JSONB,
	runtime_memory_mib INTEGER,
	runtime_gpu VARCHAR(160)[],
	runtime_gpu_count INTEGER,
	runtime_requires_gpu BOOLEAN,
	runtime_image_id VARCHAR(160),
	runtime_timeout_seconds FLOAT,
	runtime_retries INTEGER,
	runtime_keep_warm INTEGER,
	runtime_concurrency INTEGER,
	runtime_in_process BOOLEAN,
	runtime_workers INTEGER,
	runtime_checkpoint_enabled BOOLEAN,
	runtime_checkpoint_readiness_path TEXT,
	runtime_checkpoint_readiness_port INTEGER,
	runtime_checkpoint_readiness_timeout_seconds INTEGER,
	runtime_checkpoint_readiness_interval_seconds FLOAT,
	runtime_health_check_path TEXT,
	runtime_health_check_port INTEGER,
	runtime_pool_selector VARCHAR(240),
	runtime_runtime VARCHAR(80),
	runtime_runtime_class VARCHAR(160),
	runtime_docker_enabled BOOLEAN,
	runtime_block_network BOOLEAN,
	runtime_allow_list TEXT[],
	runtime_preemptible BOOLEAN,
	runtime_workspace_gpu_quota INTEGER,
	runtime_workspace_cpu_quota_millicores INTEGER,
	autoscaler_type VARCHAR(80),
	autoscaler_max_containers INTEGER,
	autoscaler_min_containers INTEGER,
	autoscaler_tasks_per_container INTEGER,
	autoscaler_failed_container_threshold INTEGER,
	autoscaler_max_failed_containers INTEGER,
	autoscaler_failure_threshold INTEGER,
	autoscaler_failed_container_window_seconds INTEGER,
	autoscaler_failure_window_seconds INTEGER,
	task_policy_timeout FLOAT,
	task_policy_timeout_seconds FLOAT,
	task_policy_ttl INTEGER,
	task_policy_ttl_seconds INTEGER,
	workspace_id UUID NOT NULL,
	app_id UUID,
	object_id UUID,
	name VARCHAR(240) NOT NULL,
	type VARCHAR(80) NOT NULL,
	public BOOLEAN NOT NULL,
	preparation_fingerprint VARCHAR(64),
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_stubs_runtime_limits CHECK (runtime_cpu_millicores >= 0 AND runtime_memory_mib >= 0 AND runtime_gpu_count >= 0 AND runtime_timeout_seconds >= 0 AND runtime_retries >= 0 AND runtime_keep_warm >= -1 AND runtime_concurrency > 0 AND runtime_workers >= 0),
	CONSTRAINT ck_stubs_autoscaler_limits CHECK (COALESCE(autoscaler_min_containers, 0) >= 0 AND COALESCE(autoscaler_max_containers, 1) >= COALESCE(autoscaler_min_containers, 0) AND autoscaler_tasks_per_container > 0),
	CONSTRAINT ck_stubs_probe_ports CHECK (runtime_checkpoint_readiness_port BETWEEN 0 AND 65535 AND runtime_health_check_port BETWEEN 0 AND 65535),
	CONSTRAINT uq_stubs_preparation_fingerprint UNIQUE (workspace_id, preparation_fingerprint),
	FOREIGN KEY(image_context_object_id) REFERENCES objects (id) ON DELETE SET NULL,
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(app_id) REFERENCES apps (id) ON DELETE SET NULL,
	FOREIGN KEY(object_id) REFERENCES objects (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_stubs_app_created ON stubs (app_id, created_at, id)
""")
    op.execute("""
CREATE INDEX ix_stubs_app_type_created ON stubs (app_id, type, created_at, id)
""")
    op.execute("""
CREATE INDEX ix_stubs_name_workspace ON stubs (name, workspace_id)
""")
    op.execute("""
CREATE INDEX ix_stubs_reusable_identity ON stubs (workspace_id, name, app_id, created_at, id) WHERE deployment_id IS NULL
""")
    op.execute("""
CREATE INDEX ix_stubs_workspace ON stubs (workspace_id)
""")
    op.execute("""
CREATE TABLE billing_credit_adjustments (
	id UUID NOT NULL,
	credit_lot_id UUID NOT NULL,
	source_id VARCHAR(255) NOT NULL,
	amount_nanos BIGINT NOT NULL,
	effective_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_billing_credit_adjustments_source UNIQUE (credit_lot_id, source_id),
	CONSTRAINT ck_billing_credit_adjustments_amount CHECK (amount_nanos <> 0),
	FOREIGN KEY(credit_lot_id) REFERENCES billing_credit_lots (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE TABLE billing_ledger_segments (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	usage_record_id UUID NOT NULL,
	segment_index INTEGER NOT NULL,
	workspace_id UUID NOT NULL,
	owner_user_id UUID NOT NULL,
	dimension VARCHAR(32) NOT NULL,
	component VARCHAR(32) NOT NULL,
	basis VARCHAR(16) NOT NULL,
	subject_type VARCHAR(32) NOT NULL,
	subject_id VARCHAR(160) NOT NULL,
	app_id VARCHAR(160) NOT NULL,
	workload_id VARCHAR(160) NOT NULL,
	task_id VARCHAR(160) NOT NULL,
	worker_id VARCHAR(160) NOT NULL,
	billing_owner VARCHAR(40) NOT NULL,
	rate_class VARCHAR(64) DEFAULT 'auto' NOT NULL,
	gpu_type VARCHAR(64) NOT NULL,
	span_started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	span_ended_at TIMESTAMP WITH TIME ZONE NOT NULL,
	segment_started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	segment_ended_at TIMESTAMP WITH TIME ZONE NOT NULL,
	duration_ms BIGINT NOT NULL,
	quantity NUMERIC(38, 9) NOT NULL,
	pricing_version VARCHAR(64) NOT NULL,
	rate_nanos_per_unit NUMERIC(30, 12) NOT NULL,
	quote_effective_at TIMESTAMP WITH TIME ZONE NOT NULL,
	quote_valid_until TIMESTAMP WITH TIME ZONE,
	cost_nanos BIGINT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_billing_ledger_segments_record UNIQUE (usage_record_id, component, segment_index),
	CONSTRAINT ck_billing_ledger_segments_dimension CHECK (dimension IN ('compute_runtime', 'network_egress', 'volume_storage')),
	CONSTRAINT ck_billing_ledger_segments_component CHECK (component IN ('container_time', 'cpu', 'memory', 'gpu', 'egress', 'volume_storage')),
	CONSTRAINT ck_billing_ledger_segments_basis CHECK (basis IN ('reserved', 'measured')),
	CONSTRAINT ck_billing_ledger_segments_interval CHECK (segment_ended_at > segment_started_at),
	CONSTRAINT ck_billing_ledger_segments_duration CHECK (duration_ms >= 0),
	CONSTRAINT ck_billing_ledger_segments_cost CHECK (cost_nanos >= 0),
	CONSTRAINT ck_billing_ledger_segments_rate CHECK (rate_nanos_per_unit >= 0),
	FOREIGN KEY(usage_record_id) REFERENCES usage_records (id) ON DELETE CASCADE,
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE RESTRICT,
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_billing_ledger_segments_account_time ON billing_ledger_segments (owner_user_id, segment_started_at)
""")
    op.execute("""
CREATE INDEX ix_billing_ledger_segments_workspace_app_time ON billing_ledger_segments (workspace_id, app_id, segment_started_at)
""")
    op.execute("""
CREATE INDEX ix_billing_ledger_segments_workspace_time ON billing_ledger_segments (workspace_id, segment_started_at)
""")
    op.execute("""
CREATE TABLE compute_units (
	warm_handoff_from UUID[] DEFAULT '{}' NOT NULL,
	provider_reconcile_attempt_at TIMESTAMP WITH TIME ZONE,
	drain_reconcile_attempt_at TIMESTAMP WITH TIME ZONE,
	workspace_id UUID NOT NULL,
	capacity_owner_id UUID NOT NULL,
	capacity_owner_kind VARCHAR(64) NOT NULL,
	capacity_owner_source VARCHAR(32) NOT NULL,
	name VARCHAR(240) NOT NULL,
	pool VARCHAR(240) NOT NULL,
	provider VARCHAR(120) NOT NULL,
	selector VARCHAR(255) NOT NULL,
	status VARCHAR(80) NOT NULL,
	source VARCHAR(80) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE,
	provider_ref VARCHAR(160) NOT NULL,
	provider_connection_id UUID,
	capacity_mode VARCHAR(32) NOT NULL,
	visibility VARCHAR(32) NOT NULL,
	region VARCHAR(64) NOT NULL,
	offer_id VARCHAR(255) NOT NULL,
	capability_key VARCHAR(255) NOT NULL,
	desired_machines BIGINT NOT NULL,
	initial_machines BIGINT NOT NULL,
	min_machines BIGINT NOT NULL,
	max_machines BIGINT NOT NULL,
	observed_machines BIGINT NOT NULL,
	generation BIGINT NOT NULL,
	phase VARCHAR(32) NOT NULL,
	provider_attributes JSONB NOT NULL,
	scaling_enabled BOOLEAN NOT NULL,
	default_eligible BOOLEAN NOT NULL,
	priority INTEGER NOT NULL,
	min_free_cpu_millicores INTEGER NOT NULL,
	min_free_memory_mib INTEGER NOT NULL,
	min_free_gpu_count INTEGER NOT NULL,
	worker_cpu_millicores INTEGER NOT NULL,
	worker_memory_mib INTEGER NOT NULL,
	worker_gpu_type VARCHAR(160) NOT NULL,
	worker_gpu_count INTEGER NOT NULL,
	worker_runtimes VARCHAR(80)[] NOT NULL,
	worker_preemptible BOOLEAN NOT NULL,
	idle_drain_timeout_seconds INTEGER NOT NULL,
	scale_up_cooldown_seconds INTEGER NOT NULL,
	scale_down_cooldown_seconds INTEGER NOT NULL,
	registration_timeout_seconds INTEGER NOT NULL,
	root_volume_gib INTEGER NOT NULL,
	fallback VARCHAR(32) NOT NULL,
	provider_resource_id VARCHAR(2048) NOT NULL,
	degraded_reason VARCHAR(512),
	degraded_at TIMESTAMP WITH TIME ZONE,
	last_capacity_failure_at TIMESTAMP WITH TIME ZONE,
	launch_attempt_baseline BIGINT NOT NULL,
	platform_fleet BOOLEAN NOT NULL,
	offer_cost_terms JSONB,
	offer_storage_mib BIGINT,
	offer_availability_zone VARCHAR(64) NOT NULL,
	supplier_cpu_unit VARCHAR(32) NOT NULL,
	supplier_cpu_count BIGINT,
	replacement_machine_id VARCHAR(160) NOT NULL,
	replacement_template_version VARCHAR(160) NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_compute_units_workspace_name UNIQUE (workspace_id, name),
	CONSTRAINT uq_compute_units_capacity_owner_id UNIQUE (capacity_owner_id),
	CONSTRAINT ck_compute_units_machine_capacity CHECK (min_machines >= 0 AND desired_machines >= min_machines AND max_machines >= desired_machines AND observed_machines >= 0 AND initial_machines >= min_machines AND max_machines >= initial_machines),
	CONSTRAINT ck_compute_units_generation CHECK (generation > 0),
	CONSTRAINT ck_compute_units_internal_provider_identity CHECK (visibility <> 'internal' OR (provider_ref <> '' AND region <> '' AND offer_id <> '' AND capability_key <> '' AND capacity_mode = 'pooled' AND capacity_owner_kind = 'pooled_provider' AND capacity_owner_id = id AND (provider_connection_id IS NOT NULL OR platform_fleet))),
	CONSTRAINT ck_compute_units_worker_shape CHECK (min_free_cpu_millicores >= 0 AND min_free_memory_mib >= 0 AND min_free_gpu_count >= 0 AND worker_cpu_millicores >= 0 AND worker_memory_mib >= 0 AND worker_gpu_count >= 0),
	CONSTRAINT ck_compute_units_worker_gpu CHECK ((worker_gpu_type = '' AND worker_gpu_count = 0) OR (worker_gpu_type <> '' AND worker_gpu_count > 0)),
	CONSTRAINT ck_compute_units_lifecycle_timeouts CHECK (idle_drain_timeout_seconds BETWEEN 60 AND 86400 AND scale_up_cooldown_seconds BETWEEN 0 AND 86400 AND scale_down_cooldown_seconds BETWEEN 0 AND 86400 AND registration_timeout_seconds BETWEEN 30 AND 3600),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(provider_connection_id) REFERENCES aws_account_connections (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_compute_units_active_provider_gpu ON compute_units (provider_ref, worker_gpu_count, desired_machines) WHERE provider_ref <> '' AND desired_machines > 0
""")
    op.execute("""
CREATE INDEX ix_compute_units_workspace_pool ON compute_units (workspace_id, pool)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_compute_units_internal_placement ON compute_units (workspace_id, provider_ref, region, capability_key, root_volume_gib) WHERE visibility = 'internal' AND provider_ref <> ''
""")
    op.execute("""
CREATE TABLE compute_join_credentials (
	user_id UUID NOT NULL,
	workspace_id UUID NOT NULL,
	capacity_owner_id UUID NOT NULL,
	pool VARCHAR(240) NOT NULL,
	machine_id VARCHAR(160) NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	created_by_token_id UUID,
	status VARCHAR(32) NOT NULL,
	max_uses BIGINT NOT NULL,
	use_count BIGINT NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_compute_join_credentials_token_hash UNIQUE (token_hash),
	CONSTRAINT ck_compute_join_credentials_use_count CHECK (max_uses > 0 AND use_count >= 0 AND use_count <= max_uses),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(created_by_token_id) REFERENCES tokens (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_compute_join_credentials_user_status ON compute_join_credentials (user_id, status)
""")
    op.execute("""
CREATE INDEX ix_compute_join_credentials_workspace_owner_status ON compute_join_credentials (workspace_id, capacity_owner_id, status)
""")
    op.execute("""
CREATE TABLE credit_purchases (
	id UUID NOT NULL,
	user_id UUID NOT NULL,
	request_key UUID NOT NULL,
	kind VARCHAR(16) NOT NULL,
	amount_nanos BIGINT NOT NULL,
	status VARCHAR(32) NOT NULL,
	provider_customer_id VARCHAR(255) NOT NULL,
	provider_session_id VARCHAR(255),
	provider_payment_id VARCHAR(255),
	success_url VARCHAR(2048) NOT NULL,
	cancel_url VARCHAR(2048) NOT NULL,
	hosted_url VARCHAR(2048),
	session_expires_at TIMESTAMP WITH TIME ZONE,
	creation_started_at TIMESTAMP WITH TIME ZONE,
	credit_lot_id UUID,
	funded_at TIMESTAMP WITH TIME ZONE,
	reversed_nanos BIGINT NOT NULL,
	reversal_sequence INTEGER NOT NULL,
	last_error VARCHAR(255) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_credit_purchases_request UNIQUE (user_id, request_key),
	CONSTRAINT uq_credit_purchases_session UNIQUE (provider_session_id),
	CONSTRAINT uq_credit_purchases_payment UNIQUE (provider_payment_id),
	CONSTRAINT uq_credit_purchases_lot UNIQUE (credit_lot_id),
	CONSTRAINT ck_credit_purchases_amount CHECK (amount_nanos > 0),
	CONSTRAINT ck_credit_purchases_kind CHECK (kind IN ('manual', 'automatic')),
	CONSTRAINT ck_credit_purchases_status CHECK (status IN ('pending', 'action_required', 'succeeded', 'declined', 'cancelled')),
	CONSTRAINT ck_credit_purchases_reversal CHECK (reversed_nanos >= 0 AND reversed_nanos <= amount_nanos),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT,
	FOREIGN KEY(credit_lot_id) REFERENCES billing_credit_lots (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_credit_purchases_reconciliation ON credit_purchases (status, updated_at)
""")
    op.execute("""
CREATE INDEX ix_credit_purchases_user_id ON credit_purchases (user_id)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_credit_purchases_pending_automatic ON credit_purchases (user_id) WHERE kind = 'automatic' AND status IN ('pending', 'action_required')
""")
    op.execute("""
CREATE TABLE workspace_audit_events (
	workspace_id UUID NOT NULL,
	actor_token_id UUID,
	actor_user_id UUID,
	action VARCHAR(80) NOT NULL,
	target_type VARCHAR(40) NOT NULL,
	target_id VARCHAR(160) NOT NULL,
	actor_name TEXT NOT NULL,
	target_name TEXT NOT NULL,
	summary TEXT NOT NULL,
	previous_value TEXT,
	new_value TEXT,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(actor_token_id) REFERENCES tokens (id) ON DELETE SET NULL,
	FOREIGN KEY(actor_user_id) REFERENCES users (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_workspace_audit_actor ON workspace_audit_events (actor_token_id, created_at)
""")
    op.execute("""
CREATE INDEX ix_workspace_audit_workspace_created ON workspace_audit_events (workspace_id, created_at, id)
""")
    op.execute("""
CREATE TABLE image_build_logs (
	build_id UUID NOT NULL,
	sequence BIGINT NOT NULL,
	message TEXT NOT NULL,
	PRIMARY KEY (build_id, sequence),
	FOREIGN KEY(build_id) REFERENCES image_builds (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE TABLE image_build_requests (
	workspace_id UUID NOT NULL,
	request_id UUID NOT NULL,
	build_id UUID NOT NULL,
	PRIMARY KEY (workspace_id, request_id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(build_id) REFERENCES image_builds (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_image_build_requests_build_id ON image_build_requests (build_id)
""")
    op.execute("""
CREATE TABLE workers (
	workspace_id UUID,
	machine_id UUID,
	pool VARCHAR(240) NOT NULL,
	status VARCHAR(80) NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE,
	admitted_release_generation BIGINT DEFAULT '0' NOT NULL,
	admitted_runtime_image VARCHAR(1024) DEFAULT '' NOT NULL,
	admitted_agent_sha256 VARCHAR(64) DEFAULT '' NOT NULL,
	update_generation BIGINT DEFAULT '0' NOT NULL,
	update_runtime_image VARCHAR(1024) DEFAULT '' NOT NULL,
	update_agent_sha256 VARCHAR(64) DEFAULT '' NOT NULL,
	update_started_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	payload JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_workers_release_generation CHECK (admitted_release_generation >= 0),
	CONSTRAINT ck_workers_update_generation CHECK (update_generation >= 0),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE SET NULL,
	FOREIGN KEY(machine_id) REFERENCES machines (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_workers_machine ON workers (machine_id)
""")
    op.execute("""
CREATE INDEX ix_workers_pool_status ON workers (pool, status)
""")
    op.execute("""
CREATE INDEX ix_workers_workspace_created ON workers (workspace_id, created_at)
""")
    op.execute("""
CREATE TABLE agent_leases (
	agent_id UUID NOT NULL,
	resource_type VARCHAR(120) NOT NULL,
	resource_id VARCHAR(160) NOT NULL,
	status VARCHAR(80) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	released_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	payload JSONB NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_agent_leases_agent_status ON agent_leases (agent_id, status)
""")
    op.execute("""
CREATE INDEX ix_agent_leases_resource ON agent_leases (resource_type, resource_id)
""")
    op.execute("""
CREATE TABLE source_cache_cleanup_targets (
	workspace_id UUID NOT NULL,
	cache_generation_id UUID NOT NULL,
	source_object_id UUID NOT NULL,
	status VARCHAR(32) NOT NULL,
	attempt_count INTEGER NOT NULL,
	next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
	claim_token VARCHAR(64),
	claim_expires_at TIMESTAMP WITH TIME ZONE,
	claim_session_fence INTEGER,
	last_error_code VARCHAR(32),
	completed_at TIMESTAMP WITH TIME ZONE,
	completion_reason VARCHAR(32),
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_source_cache_cleanup_target UNIQUE (workspace_id, cache_generation_id, source_object_id),
	CONSTRAINT ck_source_cache_cleanup_targets_status CHECK (status IN ('pending', 'claimed', 'completed')),
	CONSTRAINT ck_source_cache_cleanup_targets_attempt_count_nonnegative CHECK (attempt_count >= 0),
	CONSTRAINT ck_source_cache_cleanup_targets_claim_fence_positive CHECK (claim_session_fence IS NULL OR claim_session_fence > 0),
	CONSTRAINT ck_source_cache_cleanup_targets_completion_reason CHECK (completion_reason IS NULL OR completion_reason IN ('purged', 'storage-destroyed')),
	CONSTRAINT ck_source_cache_cleanup_targets_error_code CHECK (last_error_code IS NULL OR last_error_code IN ('purge-failed')),
	CONSTRAINT ck_source_cache_cleanup_targets_lifecycle CHECK ((status = 'pending' AND claim_token IS NULL AND claim_expires_at IS NULL AND claim_session_fence IS NULL AND completed_at IS NULL AND completion_reason IS NULL) OR (status = 'claimed' AND claim_token IS NOT NULL AND claim_expires_at IS NOT NULL AND claim_session_fence IS NOT NULL AND completed_at IS NULL AND completion_reason IS NULL) OR (status = 'completed' AND claim_token IS NULL AND claim_expires_at IS NULL AND claim_session_fence IS NULL AND completed_at IS NOT NULL AND completion_reason IS NOT NULL)),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE RESTRICT,
	FOREIGN KEY(cache_generation_id) REFERENCES worker_cache_generations (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_source_cache_cleanup_targets_claim_expiry ON source_cache_cleanup_targets (claim_expires_at)
""")
    op.execute("""
CREATE INDEX ix_source_cache_cleanup_targets_due ON source_cache_cleanup_targets (cache_generation_id, status, next_attempt_at, id)
""")
    op.execute("""
CREATE INDEX ix_source_cache_cleanup_targets_workspace ON source_cache_cleanup_targets (workspace_id, status)
""")
    op.execute("""
CREATE TABLE deployments (
	spec JSONB NOT NULL,
	pool VARCHAR(240) NOT NULL,
	workspace_id UUID NOT NULL,
	app_id UUID,
	stub_id UUID,
	name VARCHAR(240) NOT NULL,
	kind VARCHAR(80) NOT NULL,
	version INTEGER NOT NULL,
	active BOOLEAN NOT NULL,
	subdomain VARCHAR(63) NOT NULL,
	custom_hostname VARCHAR(253),
	deleted_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_deployments_workspace_app_name_version_kind UNIQUE (workspace_id, app_id, name, version, kind),
	CONSTRAINT ck_deployments_version_nonnegative CHECK (version >= 0),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(app_id) REFERENCES apps (id) ON DELETE SET NULL,
	FOREIGN KEY(stub_id) REFERENCES stubs (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_deployments_app_created ON deployments (app_id, created_at, id)
""")
    op.execute("""
CREATE INDEX ix_deployments_stub ON deployments (stub_id)
""")
    op.execute("""
CREATE INDEX ix_deployments_workspace_created ON deployments (workspace_id, created_at, id)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_deployments_custom_hostname_version_active ON deployments (custom_hostname, version) WHERE deleted_at IS NULL
""")
    op.execute("""
CREATE UNIQUE INDEX uq_deployments_subdomain_version_active ON deployments (subdomain, version) WHERE deleted_at IS NULL
""")
    op.execute("""
CREATE TABLE billing_credit_allocations (
	id UUID NOT NULL,
	credit_lot_id UUID NOT NULL,
	ledger_segment_id UUID NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	ended_at TIMESTAMP WITH TIME ZONE NOT NULL,
	amount_nanos BIGINT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_billing_credit_allocations_amount CHECK (amount_nanos > 0),
	CONSTRAINT ck_billing_credit_allocations_window CHECK (ended_at > started_at),
	FOREIGN KEY(credit_lot_id) REFERENCES billing_credit_lots (id) ON DELETE RESTRICT,
	FOREIGN KEY(ledger_segment_id) REFERENCES billing_ledger_segments (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_billing_credit_allocations_lot ON billing_credit_allocations (credit_lot_id)
""")
    op.execute("""
CREATE INDEX ix_billing_credit_allocations_segment ON billing_credit_allocations (ledger_segment_id)
""")
    op.execute("""
CREATE TABLE billing_preferences (
	user_id UUID NOT NULL,
	monthly_usage_limit_nanos BIGINT,
	reload_enabled BOOLEAN DEFAULT false NOT NULL,
	reload_threshold_cents INTEGER DEFAULT 1000 NOT NULL,
	reload_amount_cents INTEGER NOT NULL,
	reload_paused_purchase_id UUID,
	reload_pause_reason VARCHAR(32) DEFAULT '' NOT NULL,
	reload_checked_at TIMESTAMP WITH TIME ZONE,
	reload_resumed_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (user_id),
	CONSTRAINT ck_billing_preferences_usage_limit CHECK (monthly_usage_limit_nanos IS NULL OR monthly_usage_limit_nanos >= 0),
	CONSTRAINT ck_billing_preferences_reload_amounts CHECK (reload_threshold_cents >= 0 AND reload_amount_cents > 0),
	CONSTRAINT ck_billing_preferences_reload_pause CHECK ((reload_paused_purchase_id IS NULL AND reload_pause_reason = '') OR (reload_paused_purchase_id IS NOT NULL AND reload_pause_reason IN ('declined', 'action_required'))),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT,
	FOREIGN KEY(reload_paused_purchase_id) REFERENCES credit_purchases (id) ON DELETE RESTRICT
)
""")
    op.execute("""
CREATE INDEX ix_billing_preferences_reload_due ON billing_preferences (reload_checked_at, user_id) WHERE reload_enabled AND reload_paused_purchase_id IS NULL
""")
    op.execute("""
CREATE TABLE compute_capacity_operations (
	workspace_id UUID NOT NULL,
	pool_id UUID NOT NULL,
	capacity_owner_id UUID NOT NULL,
	reservation_id UUID NOT NULL,
	operation_id UUID NOT NULL,
	desired_unit BIGINT NOT NULL,
	status VARCHAR(32) NOT NULL,
	target_machine_id UUID,
	demand_container_id VARCHAR(160),
	fulfilled_at TIMESTAMP WITH TIME ZONE,
	provider_instance_id VARCHAR(255),
	previous_desired_unit BIGINT NOT NULL,
	release_desired_unit BIGINT,
	owns_capacity BOOLEAN NOT NULL,
	join_attempt INTEGER NOT NULL,
	failure_code VARCHAR(80),
	failure_count INTEGER NOT NULL,
	last_error TEXT NOT NULL,
	cpu_millicores BIGINT NOT NULL,
	memory_mib BIGINT NOT NULL,
	gpu_type VARCHAR(160) NOT NULL,
	gpu_count INTEGER NOT NULL,
	runtime VARCHAR(80) NOT NULL,
	preemptible BOOLEAN NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_compute_capacity_operations_owner_operation UNIQUE (capacity_owner_id, operation_id),
	CONSTRAINT uq_compute_capacity_operations_reservation UNIQUE (reservation_id),
	CONSTRAINT ck_compute_capacity_operations_desired_unit CHECK (desired_unit > 0 AND previous_desired_unit >= 0 AND release_desired_unit >= 0 AND join_attempt > 0 AND failure_count >= 0),
	CONSTRAINT ck_compute_capacity_operations_shape CHECK (cpu_millicores > 0 AND memory_mib > 0 AND gpu_count >= 0 AND ((gpu_type = '' AND gpu_count = 0) OR (gpu_type <> '' AND gpu_count > 0))),
	CONSTRAINT ck_compute_capacity_operations_status CHECK (status IN ('intent', 'existing_pending', 'requested', 'at_limit', 'temporarily_unavailable', 'rejected', 'unsupported', 'releasing', 'released', 'fulfilled')),
	CONSTRAINT ck_compute_capacity_operations_fulfillment CHECK (status <> 'fulfilled' OR target_machine_id IS NOT NULL),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(pool_id) REFERENCES compute_units (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_compute_capacity_operations_demand ON compute_capacity_operations (demand_container_id, created_at)
""")
    op.execute("""
CREATE INDEX ix_compute_capacity_operations_owner_status ON compute_capacity_operations (capacity_owner_id, status)
""")
    op.execute("""
CREATE TABLE compute_provider_instances (
	pool_id UUID,
	provider VARCHAR(80) NOT NULL,
	offer_id VARCHAR(255) NOT NULL,
	instance_type VARCHAR(255),
	instance_id VARCHAR(255),
	machine_id UUID,
	gpu VARCHAR(160),
	gpu_count BIGINT NOT NULL,
	cpu_millicores BIGINT NOT NULL,
	memory_mb BIGINT NOT NULL,
	committed_micros BIGINT NOT NULL,
	source VARCHAR(80) NOT NULL,
	status VARCHAR(80) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE,
	billing_renewal_at TIMESTAMP WITH TIME ZONE,
	cost_terms JSONB NOT NULL,
	storage_mib BIGINT,
	supplier_cpu_unit TEXT NOT NULL,
	supplier_cpu_count BIGINT,
	billing_started_at TIMESTAMP WITH TIME ZONE,
	bootstrap_phase TEXT NOT NULL,
	bootstrap_failure_reason TEXT,
	bootstrap_failure_detail TEXT NOT NULL,
	bootstrap_observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	bootstrap_phase_started_at TIMESTAMP WITH TIME ZONE,
	first_enrolled_at TIMESTAMP WITH TIME ZONE,
	first_served_at TIMESTAMP WITH TIME ZONE,
	last_served_at TIMESTAMP WITH TIME ZONE,
	unserved_observations BIGINT NOT NULL,
	launch_attempt BIGINT NOT NULL,
	architecture TEXT NOT NULL,
	runtime TEXT NOT NULL,
	region TEXT NOT NULL,
	availability_zone TEXT NOT NULL,
	storage_volume_ids VARCHAR(255)[] NOT NULL,
	booted_template_version TEXT NOT NULL,
	missing_since TIMESTAMP WITH TIME ZONE,
	provider_storage_destroyed_at TIMESTAMP WITH TIME ZONE,
	terminating_reason TEXT NOT NULL,
	terminated_reason TEXT NOT NULL,
	status_message TEXT NOT NULL,
	last_error TEXT NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_compute_provider_instances_capacity CHECK (gpu_count >= 0 AND cpu_millicores >= 0 AND memory_mb >= 0 AND storage_mib >= 0 AND supplier_cpu_count >= 0 AND launch_attempt > 0 AND unserved_observations >= 0),
	FOREIGN KEY(pool_id) REFERENCES compute_units (id) ON DELETE CASCADE,
	FOREIGN KEY(machine_id) REFERENCES machines (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_compute_provider_instances_pool ON compute_provider_instances (pool_id)
""")
    op.execute("""
CREATE INDEX ix_compute_provider_instances_pool_status ON compute_provider_instances (pool_id, status)
""")
    op.execute("""
CREATE INDEX ix_compute_provider_instances_renewal ON compute_provider_instances (billing_renewal_at)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_compute_provider_instances_machine ON compute_provider_instances (machine_id) WHERE machine_id IS NOT NULL
""")
    op.execute("""
CREATE UNIQUE INDEX uq_compute_provider_instances_pool_instance ON compute_provider_instances (pool_id, instance_id) WHERE pool_id IS NOT NULL AND instance_id IS NOT NULL
""")
    op.execute("""
CREATE TABLE compute_machine_enrollments (
	tunnel_public_key_sha256 VARCHAR(64) NOT NULL,
	capacity_reason TEXT NOT NULL,
	capacity_notice_at TIMESTAMP WITH TIME ZONE,
	hostname VARCHAR(255) NOT NULL,
	os VARCHAR(160) NOT NULL,
	arch VARCHAR(64) NOT NULL,
	cpu_count INTEGER NOT NULL,
	cpu_millicores BIGINT NOT NULL,
	memory_mb BIGINT NOT NULL,
	gpus VARCHAR(160)[] NOT NULL,
	gpu_ids VARCHAR(160)[] NOT NULL,
	gpu_count INTEGER NOT NULL,
	executor VARCHAR(160) NOT NULL,
	preflight_checks JSONB NOT NULL,
	agent_version VARCHAR(160) NOT NULL,
	user_id UUID NOT NULL,
	workspace_id UUID NOT NULL,
	capacity_owner_id UUID NOT NULL,
	pool VARCHAR(240) NOT NULL,
	machine_id UUID NOT NULL,
	machine_fingerprint_hash VARCHAR(64) NOT NULL,
	join_credential_id UUID,
	credential_hash VARCHAR(64) NOT NULL,
	credential_generation BIGINT NOT NULL,
	status VARCHAR(32) NOT NULL,
	preflight_passed BOOLEAN NOT NULL,
	heartbeat_confirmed BOOLEAN NOT NULL,
	schedulable BOOLEAN NOT NULL,
	capacity_state VARCHAR(32) NOT NULL,
	capacity_observed_at TIMESTAMP WITH TIME ZONE,
	readiness_phase VARCHAR(32) NOT NULL,
	last_join_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_heartbeat_at TIMESTAMP WITH TIME ZONE,
	last_disconnect_at TIMESTAMP WITH TIME ZONE,
	revoked_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_compute_machine_enrollments_machine UNIQUE (workspace_id, machine_id),
	CONSTRAINT uq_compute_machine_enrollments_fingerprint UNIQUE (user_id, machine_fingerprint_hash),
	CONSTRAINT uq_compute_machine_enrollments_credential_hash UNIQUE (credential_hash),
	CONSTRAINT ck_compute_machine_enrollments_generation CHECK (credential_generation > 0),
	CONSTRAINT ck_compute_machine_enrollments_capacity CHECK (cpu_count >= 0 AND cpu_millicores >= 0 AND memory_mb >= 0 AND gpu_count >= 0),
	CONSTRAINT ck_compute_machine_enrollments_tunnel_key CHECK (tunnel_public_key_sha256 = '' OR tunnel_public_key_sha256 ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_compute_machine_enrollments_capacity_state CHECK (capacity_state IN ('available', 'draining', 'preempting', 'cordoned')),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(machine_id) REFERENCES machines (id) ON DELETE CASCADE,
	FOREIGN KEY(join_credential_id) REFERENCES compute_join_credentials (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_compute_machine_enrollments_status_last_join ON compute_machine_enrollments (status, last_join_at)
""")
    op.execute("""
CREATE INDEX ix_compute_machine_enrollments_user_status ON compute_machine_enrollments (user_id, status)
""")
    op.execute("""
CREATE INDEX ix_compute_machine_enrollments_workspace_owner_status ON compute_machine_enrollments (workspace_id, capacity_owner_id, status)
""")
    op.execute("""
CREATE TABLE autoscaling_targets (
	stub_id UUID NOT NULL,
	workspace_id UUID NOT NULL,
	target_kind VARCHAR(80) NOT NULL,
	due_at TIMESTAMP WITH TIME ZONE NOT NULL,
	generation BIGINT NOT NULL,
	claim_token VARCHAR(128),
	claim_expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (stub_id),
	CONSTRAINT ck_autoscaling_targets_generation CHECK (generation >= 1),
	CONSTRAINT ck_autoscaling_targets_kind CHECK (target_kind IN ('function', 'endpoint', 'pod')),
	CONSTRAINT ck_autoscaling_targets_claim CHECK ((claim_token IS NULL) = (claim_expires_at IS NULL)),
	FOREIGN KEY(stub_id) REFERENCES stubs (id) ON DELETE CASCADE,
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_autoscaling_targets_due ON autoscaling_targets (due_at, stub_id)
""")
    op.execute("""
CREATE INDEX ix_autoscaling_targets_workspace ON autoscaling_targets (workspace_id)
""")
    op.execute("""
CREATE TABLE containers (
	workload_ready_at TIMESTAMP WITH TIME ZONE,
	scheduling_request JSONB,
	scheduling_reconcile_at TIMESTAMP WITH TIME ZONE,
	scheduling_assigned_at TIMESTAMP WITH TIME ZONE,
	scheduling_assignment_token VARCHAR(240),
	capacity_retry_at TIMESTAMP WITH TIME ZONE,
	workspace_id UUID NOT NULL,
	stub_id UUID,
	app_id UUID,
	machine_id UUID,
	worker_id UUID,
	task_id UUID,
	name VARCHAR(240) NOT NULL,
	image VARCHAR(512) NOT NULL,
	status VARCHAR(80) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE,
	exit_code INTEGER,
	termination_reason VARCHAR(80) NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	storage_released_at TIMESTAMP WITH TIME ZONE,
	preemption_settled_at TIMESTAMP WITH TIME ZONE,
	gpu_count INTEGER NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	payload JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_containers_termination_reason CHECK (termination_reason IN ('TTL', 'USER', 'SCHEDULER', 'PREEMPTED', 'ADMIN', 'UNFUNDED', 'MEMORY_EVICTED', 'UNKNOWN')),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(stub_id) REFERENCES stubs (id) ON DELETE SET NULL,
	FOREIGN KEY(app_id) REFERENCES apps (id) ON DELETE SET NULL,
	FOREIGN KEY(machine_id) REFERENCES machines (id) ON DELETE SET NULL,
	FOREIGN KEY(worker_id) REFERENCES workers (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_containers_capacity_due ON containers (capacity_retry_at, id) WHERE capacity_retry_at IS NOT NULL AND status = 'pending'
""")
    op.execute("""
CREATE INDEX ix_containers_live_expiry ON containers (expires_at, id) WHERE expires_at IS NOT NULL AND status IN ('pending', 'running')
""")
    op.execute("""
CREATE INDEX ix_containers_machine_status ON containers (machine_id, status)
""")
    op.execute("""
CREATE INDEX ix_containers_pending_storage_worker ON containers ((payload ->> 'runtime_worker_id'), id) WHERE storage_released_at IS NULL
""")
    op.execute("""
CREATE INDEX ix_containers_scheduling_due ON containers (scheduling_reconcile_at, id) WHERE scheduling_request IS NOT NULL AND status = 'pending'
""")
    op.execute("""
CREATE INDEX ix_containers_status_created ON containers (status, created_at, id)
""")
    op.execute("""
CREATE INDEX ix_containers_stub ON containers (stub_id)
""")
    op.execute("""
CREATE INDEX ix_containers_stub_failed_created ON containers (stub_id, created_at, id) WHERE status = 'failed'
""")
    op.execute("""
CREATE INDEX ix_containers_stub_failed_finished ON containers (stub_id, finished_at, id) WHERE status = 'failed' AND finished_at IS NOT NULL
""")
    op.execute("""
CREATE INDEX ix_containers_stub_live ON containers (stub_id, created_at, id) WHERE status IN ('pending', 'running')
""")
    op.execute("""
CREATE INDEX ix_containers_unsettled_preemption ON containers (finished_at) WHERE termination_reason = 'PREEMPTED' AND preemption_settled_at IS NULL
""")
    op.execute("""
CREATE INDEX ix_containers_worker_status ON containers (worker_id, status)
""")
    op.execute("""
CREATE INDEX ix_containers_workspace_created ON containers (workspace_id, created_at)
""")
    op.execute("""
CREATE INDEX ix_containers_workspace_live ON containers (workspace_id) WHERE status IN ('pending', 'running')
""")
    op.execute("""
CREATE TABLE provider_node_launches (
	id UUID NOT NULL,
	workspace_id UUID NOT NULL,
	unit_id UUID NOT NULL,
	provider_ref VARCHAR(255) NOT NULL,
	region VARCHAR(64) NOT NULL,
	generation INTEGER NOT NULL,
	server_name VARCHAR(255) NOT NULL,
	provider_instance_id VARCHAR(128),
	bootstrap_token_hash VARCHAR(64) NOT NULL,
	bootstrap_token_ciphertext TEXT,
	node_token_hash VARCHAR(64),
	fingerprint_hash VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	redeemed_at TIMESTAMP WITH TIME ZONE,
	revoked_at TIMESTAMP WITH TIME ZONE,
	enrolled_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT ck_provider_node_launches_expiry CHECK (expires_at > created_at),
	CONSTRAINT ck_provider_node_launches_generation CHECK (generation > 0),
	CONSTRAINT ck_provider_node_launches_redemption CHECK ((redeemed_at IS NULL AND node_token_hash IS NULL) OR (redeemed_at IS NOT NULL AND node_token_hash IS NOT NULL AND bootstrap_token_ciphertext IS NULL)),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(unit_id) REFERENCES compute_units (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_provider_node_launches_active_instance ON provider_node_launches (provider_ref, provider_instance_id) WHERE revoked_at IS NULL AND provider_instance_id IS NOT NULL
""")
    op.execute("""
CREATE UNIQUE INDEX uq_provider_node_launches_active_slot ON provider_node_launches (unit_id, server_name) WHERE revoked_at IS NULL
""")
    op.execute("""
CREATE UNIQUE INDEX uq_provider_node_launches_node_hash ON provider_node_launches (node_token_hash)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_provider_node_launches_token_hash ON provider_node_launches (bootstrap_token_hash)
""")
    op.execute("""
CREATE TABLE app_deployment_intents (
	app_id UUID NOT NULL,
	deployment_id UUID NOT NULL,
	operation_revision BIGINT NOT NULL,
	target VARCHAR(16) NOT NULL,
	event_id UUID,
	event_created_at TIMESTAMP WITH TIME ZONE,
	workspace_change_published_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (app_id, deployment_id),
	CONSTRAINT ck_app_deployment_intents_revision CHECK (operation_revision >= 0),
	CONSTRAINT ck_app_deployment_intents_target CHECK (target IN ('active', 'inactive', 'deleted')),
	CONSTRAINT ck_app_deployment_intents_publication CHECK ((event_id IS NULL) = (event_created_at IS NULL) AND (workspace_change_published_at IS NULL OR event_id IS NOT NULL)),
	FOREIGN KEY(app_id) REFERENCES apps (id) ON DELETE CASCADE,
	FOREIGN KEY(deployment_id) REFERENCES deployments (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_app_deployment_intents_deployment ON app_deployment_intents (deployment_id)
""")
    op.execute("""
CREATE TABLE app_container_shutdown_intents (
	app_id UUID NOT NULL,
	container_id UUID NOT NULL,
	worker_id VARCHAR(240) NOT NULL,
	operation_revision BIGINT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (app_id, container_id),
	CONSTRAINT ck_app_container_shutdown_intents_revision CHECK (operation_revision >= 0),
	FOREIGN KEY(app_id) REFERENCES apps (id) ON DELETE CASCADE,
	FOREIGN KEY(container_id) REFERENCES containers (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_app_container_shutdown_intents_container ON app_container_shutdown_intents (container_id)
""")
    op.execute("""
CREATE TABLE cron_jobs (
	workspace_id UUID NOT NULL,
	deployment_id UUID NOT NULL,
	name VARCHAR(240) NOT NULL,
	cron VARCHAR(160) NOT NULL,
	enabled BOOLEAN NOT NULL,
	last_run_at TIMESTAMP WITH TIME ZONE,
	next_run_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_cron_jobs_workspace_name UNIQUE (workspace_id, name),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(deployment_id) REFERENCES deployments (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_cron_jobs_deployment ON cron_jobs (deployment_id)
""")
    op.execute("""
CREATE INDEX ix_cron_jobs_due ON cron_jobs (next_run_at, id) WHERE enabled IS TRUE
""")
    op.execute("""
CREATE INDEX ix_cron_jobs_workspace ON cron_jobs (workspace_id)
""")
    op.execute("""
CREATE TABLE container_rollout_drains (
	container_id UUID NOT NULL,
	stub_id UUID NOT NULL,
	serving_floor INTEGER NOT NULL,
	requested_at TIMESTAMP WITH TIME ZONE NOT NULL,
	admission_closed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (container_id),
	CONSTRAINT ck_container_rollout_drains_serving_floor CHECK (serving_floor >= 0),
	FOREIGN KEY(container_id) REFERENCES containers (id) ON DELETE CASCADE,
	FOREIGN KEY(stub_id) REFERENCES stubs (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_container_rollout_drains_stub ON container_rollout_drains (stub_id)
""")
    op.execute("""
CREATE TABLE tasks (
	handler TEXT,
	command TEXT[] NOT NULL,
	args JSONB NOT NULL,
	kwargs JSONB NOT NULL,
	input_container_id VARCHAR(160),
	invocation JSONB,
	dependency_bindings JSONB NOT NULL,
	function_result JSONB,
	result JSONB,
	error TEXT,
	exit_code INTEGER,
	retry_backoff VARCHAR(20),
	retry_delay_seconds FLOAT,
	retry_max_delay_seconds FLOAT,
	retry_on_statuses VARCHAR(80)[],
	workspace_id UUID,
	app_id UUID,
	stub_id UUID,
	deployment_id UUID,
	container_id UUID,
	parent_task_id UUID,
	root_task_id UUID,
	name VARCHAR(240) NOT NULL,
	status VARCHAR(80) NOT NULL,
	attempt_number INTEGER NOT NULL,
	max_attempts INTEGER NOT NULL,
	next_retry_at TIMESTAMP WITH TIME ZONE,
	claimable_at TIMESTAMP WITH TIME ZONE,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_tasks_attempts CHECK (attempt_number >= 0 AND max_attempts >= 1),
	CONSTRAINT ck_tasks_retry_backoff CHECK (retry_backoff IS NULL OR retry_backoff IN ('fixed', 'exponential')),
	CONSTRAINT ck_tasks_retry_delay CHECK (retry_delay_seconds >= 0 AND (retry_max_delay_seconds IS NULL OR retry_max_delay_seconds >= retry_delay_seconds)),
	CONSTRAINT ck_tasks_retry_policy CHECK ((retry_backoff IS NULL AND retry_delay_seconds IS NULL AND retry_max_delay_seconds IS NULL AND retry_on_statuses IS NULL) OR (retry_backoff IS NOT NULL AND retry_delay_seconds IS NOT NULL AND retry_on_statuses IS NOT NULL)),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE SET NULL,
	FOREIGN KEY(app_id) REFERENCES apps (id) ON DELETE SET NULL,
	FOREIGN KEY(stub_id) REFERENCES stubs (id) ON DELETE SET NULL,
	FOREIGN KEY(deployment_id) REFERENCES deployments (id) ON DELETE SET NULL,
	FOREIGN KEY(container_id) REFERENCES containers (id) ON DELETE SET NULL,
	FOREIGN KEY(parent_task_id) REFERENCES tasks (id) ON DELETE SET NULL,
	FOREIGN KEY(root_task_id) REFERENCES tasks (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_tasks_container ON tasks (container_id)
""")
    op.execute("""
CREATE INDEX ix_tasks_input_container ON tasks (input_container_id)
""")
    op.execute("""
CREATE INDEX ix_tasks_parent_created ON tasks (parent_task_id, created_at, id)
""")
    op.execute("""
CREATE INDEX ix_tasks_retry_due ON tasks (status, next_retry_at)
""")
    op.execute("""
CREATE INDEX ix_tasks_root_created ON tasks (root_task_id, created_at, id)
""")
    op.execute("""
CREATE INDEX ix_tasks_status_created ON tasks (status, created_at)
""")
    op.execute("""
CREATE INDEX ix_tasks_stub_claimable ON tasks (stub_id, status, claimable_at)
""")
    op.execute("""
CREATE INDEX ix_tasks_stub_created ON tasks (stub_id, created_at, id)
""")
    op.execute("""
CREATE INDEX ix_tasks_workspace_created ON tasks (workspace_id, created_at, id)
""")
    op.execute("""
CREATE TABLE pod_urls (
	container_id UUID NOT NULL,
	port INTEGER NOT NULL,
	url TEXT NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_pod_urls_container_port UNIQUE (container_id, port),
	CONSTRAINT ck_pod_urls_port_range CHECK (port >= 1 AND port <= 65535),
	FOREIGN KEY(container_id) REFERENCES containers (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_pod_urls_container ON pod_urls (container_id)
""")
    op.execute("""
CREATE TABLE checkpoints (
	checkpoint_id VARCHAR(255) NOT NULL,
	source_container_id UUID,
	container_ip VARCHAR(120) NOT NULL,
	exposed_ports INTEGER[] NOT NULL,
	status VARCHAR(80) NOT NULL,
	remote_key TEXT NOT NULL,
	workspace_id UUID,
	stub_id UUID,
	stub_type VARCHAR(80) NOT NULL,
	app_id UUID,
	cache_hash VARCHAR(255) NOT NULL,
	cache_size_bytes BIGINT NOT NULL,
	origin_key TEXT NOT NULL,
	locality VARCHAR(160) NOT NULL,
	accelerator VARCHAR(160) NOT NULL,
	last_restored_at TIMESTAMP WITH TIME ZONE,
	retention_expires_at TIMESTAMP WITH TIME ZONE,
	cleanup_claimed_at TIMESTAMP WITH TIME ZONE,
	deleted_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_checkpoints_checkpoint_id UNIQUE (checkpoint_id),
	CONSTRAINT ck_checkpoints_cache_size_nonnegative CHECK (cache_size_bytes >= 0),
	FOREIGN KEY(source_container_id) REFERENCES containers (id) ON DELETE SET NULL,
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE SET NULL,
	FOREIGN KEY(stub_id) REFERENCES stubs (id) ON DELETE SET NULL,
	FOREIGN KEY(app_id) REFERENCES apps (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_checkpoints_cache_hash ON checkpoints (cache_hash)
""")
    op.execute("""
CREATE INDEX ix_checkpoints_cleanup_claimed_at ON checkpoints (cleanup_claimed_at)
""")
    op.execute("""
CREATE INDEX ix_checkpoints_retention_expires_at ON checkpoints (retention_expires_at)
""")
    op.execute("""
CREATE INDEX ix_checkpoints_stub_created ON checkpoints (stub_id, created_at)
""")
    op.execute("""
CREATE INDEX ix_checkpoints_workspace_created ON checkpoints (workspace_id, created_at)
""")
    op.execute("""
CREATE TABLE endpoint_dispatches (
	task_id UUID NOT NULL,
	workspace_id UUID NOT NULL,
	stub_id UUID NOT NULL,
	container_id UUID,
	method VARCHAR(32) NOT NULL,
	path TEXT NOT NULL,
	status VARCHAR(32) NOT NULL,
	wait_timeout_seconds FLOAT NOT NULL,
	max_pending_requests INTEGER NOT NULL,
	max_inflight_per_container INTEGER NOT NULL,
	attempts INTEGER NOT NULL,
	enqueued_at TIMESTAMP WITH TIME ZONE NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	heartbeat_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	error TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (task_id),
	CONSTRAINT ck_endpoint_dispatches_status CHECK (status IN ('queued', 'waiting-capacity', 'inflight', 'complete', 'failed', 'timeout', 'cancelled')),
	CONSTRAINT ck_endpoint_dispatches_wait_timeout CHECK (wait_timeout_seconds > 0),
	CONSTRAINT ck_endpoint_dispatches_max_pending CHECK (max_pending_requests > 0),
	CONSTRAINT ck_endpoint_dispatches_max_inflight CHECK (max_inflight_per_container > 0),
	CONSTRAINT ck_endpoint_dispatches_attempts CHECK (attempts >= 0),
	FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE,
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(stub_id) REFERENCES stubs (id) ON DELETE CASCADE,
	FOREIGN KEY(container_id) REFERENCES containers (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_endpoint_dispatches_stub_active_expiry ON endpoint_dispatches (stub_id, expires_at) WHERE status IN ('queued', 'waiting-capacity', 'inflight')
""")
    op.execute("""
CREATE INDEX ix_endpoint_dispatches_stub_container_finished ON endpoint_dispatches (stub_id, container_id, finished_at) WHERE container_id IS NOT NULL AND finished_at IS NOT NULL
""")
    op.execute("""
CREATE INDEX ix_endpoint_dispatches_stub_container_inflight ON endpoint_dispatches (stub_id, container_id, expires_at) WHERE status = 'inflight'
""")
    op.execute("""
CREATE TABLE task_attempts (
	result JSONB,
	error TEXT,
	exit_code INTEGER,
	workspace_id UUID,
	task_id UUID NOT NULL,
	container_id UUID,
	attempt_number INTEGER NOT NULL,
	status VARCHAR(80) NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_task_attempts_number CHECK (attempt_number >= 1),
	CONSTRAINT uq_task_attempts_task_attempt UNIQUE (task_id, attempt_number),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE SET NULL,
	FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE,
	FOREIGN KEY(container_id) REFERENCES containers (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_task_attempts_container ON task_attempts (container_id)
""")
    op.execute("""
CREATE INDEX ix_task_attempts_status_created ON task_attempts (status, created_at)
""")
    op.execute("""
CREATE INDEX ix_task_attempts_task ON task_attempts (task_id, attempt_number)
""")
    op.execute("""
CREATE INDEX ix_task_attempts_workspace_created ON task_attempts (workspace_id, created_at)
""")
    op.execute("""
CREATE TABLE task_dependencies (
	workspace_id UUID,
	task_id UUID NOT NULL,
	upstream_task_id UUID NOT NULL,
	parent_task_id UUID,
	root_task_id UUID,
	edge_type VARCHAR(80) NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE,
	FOREIGN KEY(upstream_task_id) REFERENCES tasks (id) ON DELETE CASCADE,
	FOREIGN KEY(parent_task_id) REFERENCES tasks (id) ON DELETE SET NULL,
	FOREIGN KEY(root_task_id) REFERENCES tasks (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_task_dependencies_task ON task_dependencies (task_id, upstream_task_id)
""")
    op.execute("""
CREATE INDEX ix_task_dependencies_upstream ON task_dependencies (upstream_task_id, task_id)
""")
    op.execute("""
CREATE INDEX ix_task_dependencies_workspace_root ON task_dependencies (workspace_id, root_task_id)
""")
    op.execute("""
CREATE TABLE logs (
	workspace_id UUID,
	task_id UUID,
	container_id UUID,
	app_id UUID,
	deployment_id UUID,
	stub_id UUID,
	machine_id VARCHAR(160),
	worker_id VARCHAR(160),
	stream VARCHAR(40) NOT NULL,
	message TEXT NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE SET NULL,
	FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE INDEX ix_logs_container_created ON logs (container_id, created_at, id)
""")
    op.execute("""
CREATE INDEX ix_logs_created ON logs (created_at, id)
""")
    op.execute("""
CREATE INDEX ix_logs_task_created ON logs (task_id, created_at, id)
""")
    op.execute("""
CREATE INDEX ix_logs_workspace_created ON logs (workspace_id, created_at, id)
""")
    op.execute("""
CREATE TABLE cron_job_runs (
	reason TEXT,
	workspace_id UUID NOT NULL,
	cron_job VARCHAR(240) NOT NULL,
	enqueued BOOLEAN NOT NULL,
	task_id UUID,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE SET NULL
)
""")
    op.execute("""
CREATE INDEX ix_cron_job_runs_cron_job_created ON cron_job_runs (cron_job, created_at, id)
""")
    op.execute("""
CREATE INDEX ix_cron_job_runs_workspace_created ON cron_job_runs (workspace_id, created_at, id)
""")
    op.execute("""
ALTER TABLE apps ADD CONSTRAINT fk_apps_stub_id FOREIGN KEY(stub_id) REFERENCES stubs (id) ON DELETE SET NULL
""")
    op.execute("""
ALTER TABLE containers ADD CONSTRAINT fk_containers_task_id FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE SET NULL
""")
    op.execute("""
ALTER TABLE identity_admin_recovery_requests ADD CONSTRAINT fk_identity_admin_recovery_requests_admin_token_id FOREIGN KEY(admin_token_id) REFERENCES tokens (id) ON DELETE SET NULL
""")
    op.execute("""
ALTER TABLE identity_admin_recovery_requests ADD CONSTRAINT fk_identity_admin_recovery_requests_workspace_id FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE SET NULL
""")
    op.execute("""
ALTER TABLE identity_bootstrap_claims ADD CONSTRAINT fk_identity_bootstrap_claims_admin_token_id FOREIGN KEY(admin_token_id) REFERENCES tokens (id) ON DELETE SET NULL
""")
    op.execute("""
ALTER TABLE stubs ADD CONSTRAINT fk_stubs_deployment_id FOREIGN KEY(deployment_id) REFERENCES deployments (id) ON DELETE SET NULL
""")
    op.execute("""
ALTER TABLE workspaces ADD CONSTRAINT fk_workspaces_concurrency_limit_id FOREIGN KEY(concurrency_limit_id) REFERENCES concurrency_limits (id) ON DELETE SET NULL
""")
    op.execute("""
ALTER TABLE workspaces ADD CONSTRAINT fk_workspaces_primary_token_id FOREIGN KEY(primary_token_id) REFERENCES tokens (id) ON DELETE SET NULL
""")
    op.execute("""
CREATE OR REPLACE FUNCTION enforce_compute_capacity_ownership()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IN ('released', 'fulfilled', 'unsupported') AND (
        NEW.status IS DISTINCT FROM OLD.status
        OR NEW.target_machine_id IS DISTINCT FROM OLD.target_machine_id
        OR (
            NOT OLD.owns_capacity
            AND NEW.owns_capacity
        )
    ) THEN
        RAISE EXCEPTION 'terminal capacity ownership cannot be reopened'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER compute_capacity_ownership_fence
BEFORE UPDATE ON compute_capacity_operations
FOR EACH ROW EXECUTE FUNCTION enforce_compute_capacity_ownership();
""")
    op.execute("""
CREATE OR REPLACE FUNCTION enforce_container_assignment_ownership()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.status IN ('exited', 'failed', 'stopped') AND NEW.status IN ('pending', 'running'))
        OR (OLD.status = 'running' AND NEW.status = 'pending') THEN
        RAISE EXCEPTION 'container status cannot be reopened'
            USING ERRCODE = '23514';
    END IF;
    IF COALESCE(OLD.payload->>'runtime_worker_id', '') <> '' AND (
        COALESCE(NEW.payload->>'runtime_worker_id', '')
            <> COALESCE(OLD.payload->>'runtime_worker_id', '')
        OR COALESCE(NEW.payload->>'runtime_machine_id', '')
            <> COALESCE(OLD.payload->>'runtime_machine_id', '')
    ) AND NOT (
        OLD.status = 'pending' AND NEW.status = 'pending'
        AND COALESCE(NEW.payload->>'runtime_worker_id', '') = ''
        AND COALESCE(NEW.payload->>'runtime_machine_id', '') = ''
        AND OLD.scheduling_assignment_token IS NOT NULL
        AND NEW.scheduling_assignment_token IS NULL
    ) THEN
        RAISE EXCEPTION 'container assignment cannot change without its ownership token'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER container_assignment_ownership_fence
BEFORE UPDATE ON containers
FOR EACH ROW EXECUTE FUNCTION enforce_container_assignment_ownership();
""")
    op.execute("""
CREATE FUNCTION retain_log_attribution()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    source_task tasks%ROWTYPE;
    source_container containers%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        NEW.container_id := OLD.container_id;
        NEW.app_id := OLD.app_id;
        NEW.deployment_id := OLD.deployment_id;
        NEW.stub_id := OLD.stub_id;
        NEW.machine_id := OLD.machine_id;
        NEW.worker_id := OLD.worker_id;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.task_id IS NOT NULL AND NEW.container_id IS NULL THEN
        SELECT * INTO source_task FROM tasks
        WHERE id = NEW.task_id AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id;
        NEW.container_id := COALESCE(NEW.container_id, source_task.container_id);
        NEW.app_id := COALESCE(NEW.app_id, source_task.app_id);
        NEW.deployment_id := COALESCE(NEW.deployment_id, source_task.deployment_id);
        NEW.stub_id := COALESCE(NEW.stub_id, source_task.stub_id);
    END IF;
    IF TG_OP = 'INSERT' AND NEW.container_id IS NOT NULL
        AND (NEW.machine_id IS NULL OR NEW.worker_id IS NULL) THEN
        SELECT * INTO source_container FROM containers
        WHERE id = NEW.container_id AND workspace_id IS NOT DISTINCT FROM NEW.workspace_id;
        NEW.machine_id := COALESCE(
            NEW.machine_id,
            NULLIF(source_container.payload->>'runtime_machine_id', ''),
            source_container.machine_id::text
        );
        NEW.worker_id := COALESCE(
            NEW.worker_id,
            NULLIF(source_container.payload->>'runtime_worker_id', ''),
            source_container.worker_id::text
        );
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER logs_retain_attribution
BEFORE INSERT OR UPDATE ON logs
FOR EACH ROW EXECUTE FUNCTION retain_log_attribution();
""")


def downgrade() -> None:
    raise NotImplementedError("the initial schema is not reversible")
