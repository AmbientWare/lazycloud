"""Withdraw the unused September rate schedule before immediate publication."""

from alembic import op
from sqlalchemy import text

revision = "0030_withdraw_rate_schedule"
down_revision = "0029_remove_purchase_ceilings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        text(
            "LOCK TABLE billing_compute_rates, billing_platform_rates, "
            "billing_ledger_segments IN SHARE ROW EXCLUSIVE MODE"
        )
    )
    if connection.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM billing_ledger_segments "
            "WHERE pricing_version IN ('2026-09-04.a', '2026-09-12.a'))"
        )
    ):
        raise RuntimeError("cannot withdraw September rates that have already priced usage")
    for table, dimensions in (
        ("billing_compute_rates", ("billing_owner", "rate_class", "gpu_type")),
        ("billing_platform_rates", ()),
    ):
        ids = connection.scalars(
            text(
                f"SELECT id FROM {table} WHERE "
                "(pricing_version = '2026-09-04.a' AND effective_at = '2026-09-11T00:00:00Z') "
                "OR (pricing_version = '2026-09-12.a' AND effective_at = '2026-09-12T00:00:00Z') "
                "ORDER BY effective_at DESC"
            )
        ).all()
        for rate_id in ids:
            row = (
                connection.execute(
                    text(f"DELETE FROM {table} WHERE id = :id RETURNING *"), {"id": rate_id}
                )
                .mappings()
                .one()
            )
            subject = "".join(f" AND {key} = :{key}" for key in dimensions)
            connection.execute(
                text(f"UPDATE {table} SET valid_until = :end WHERE valid_until = :start{subject}"),
                {
                    "start": row["effective_at"],
                    "end": row["valid_until"],
                    **{key: row[key] for key in dimensions},
                },
            )


def downgrade() -> None:
    raise RuntimeError("withdrawn price schedules must not replace the active rate card")
