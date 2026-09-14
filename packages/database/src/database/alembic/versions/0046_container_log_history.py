"""Retain log attribution independently of a task's current container."""

import sqlalchemy as sa
from alembic import op

revision = "0046_container_log_history"
down_revision = "0045_outbound_agent_tunnels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("logs", "task_id", existing_type=sa.Uuid(), nullable=True)
    for name in ("container_id", "app_id", "deployment_id", "stub_id"):
        op.add_column("logs", sa.Column(name, sa.Uuid(), nullable=True))
    for name in ("machine_id", "worker_id"):
        op.add_column("logs", sa.Column(name, sa.String(160), nullable=True))
    op.execute(
        """
        UPDATE logs AS log
        SET container_id = task.container_id,
            app_id = task.app_id,
            deployment_id = task.deployment_id,
            stub_id = task.stub_id,
            machine_id = COALESCE(NULLIF(container.payload->>'runtime_machine_id', ''),
                                  container.machine_id::text),
            worker_id = COALESCE(NULLIF(container.payload->>'runtime_worker_id', ''),
                                 container.worker_id::text),
            payload = log.payload || jsonb_build_object(
                'container_id', task.container_id,
                'app_id', task.app_id,
                'deployment_id', task.deployment_id,
                'stub_id', task.stub_id,
                'machine_id', COALESCE(NULLIF(container.payload->>'runtime_machine_id', ''),
                                       container.machine_id::text),
                'worker_id', COALESCE(NULLIF(container.payload->>'runtime_worker_id', ''),
                                      container.worker_id::text)
            )
        FROM tasks AS task
        LEFT JOIN containers AS container ON container.id = task.container_id
        WHERE log.task_id = task.id
        """
    )
    op.create_index("ix_logs_container_created", "logs", ["container_id", "created_at", "id"])


def downgrade() -> None:
    raise RuntimeError("taskless log history cannot be represented by the previous schema")
