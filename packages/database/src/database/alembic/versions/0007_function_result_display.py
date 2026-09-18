"""Carry stored result previews as display text."""

from alembic import op

revision = "0007_function_result_display"
down_revision = "0006_object_uploads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE tasks
        SET function_result = (function_result - 'preview')
            || CASE
                WHEN jsonb_typeof(function_result -> 'preview') = 'string'
                THEN jsonb_build_object(
                    'display', jsonb_build_object('text', function_result -> 'preview')
                )
                ELSE '{}'::jsonb
            END
        WHERE function_result ? 'preview'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE tasks
        SET function_result = (function_result - 'display')
            || CASE
                WHEN jsonb_typeof(function_result -> 'display' -> 'text') = 'string'
                THEN jsonb_build_object(
                    'preview', left(function_result -> 'display' ->> 'text', 4096)
                )
                ELSE '{}'::jsonb
            END
        WHERE function_result ? 'display'
        """
    )
