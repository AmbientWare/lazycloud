"""Capture log attribution at insertion and preserve it on replay."""

from alembic import op

revision = "0047_log_attribution"
down_revision = "0046_container_log_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
    NEW.payload := NEW.payload || jsonb_build_object(
        'container_id', NEW.container_id,
        'app_id', NEW.app_id,
        'deployment_id', NEW.deployment_id,
        'stub_id', NEW.stub_id,
        'machine_id', NEW.machine_id,
        'worker_id', NEW.worker_id
    );
    RETURN NEW;
END;
$$;
CREATE TRIGGER logs_retain_attribution
BEFORE INSERT OR UPDATE ON logs
FOR EACH ROW EXECUTE FUNCTION retain_log_attribution();
""")


def downgrade() -> None:
    op.execute("DROP TRIGGER logs_retain_attribution ON logs")
    op.execute("DROP FUNCTION retain_log_attribution()")
