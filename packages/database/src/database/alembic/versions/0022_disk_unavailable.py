"""Let a container stop because one of its disks could not be read from storage."""

from alembic import op

revision = "0022_disk_unavailable"
down_revision = "0021_reserve_release"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_containers_termination_reason", "containers", type_="check")
    op.create_check_constraint(
        "ck_containers_termination_reason",
        "containers",
        "termination_reason IN ('TTL', 'USER', 'SCHEDULER', 'PREEMPTED', 'ADMIN', 'UNFUNDED', "
        "'MEMORY_EVICTED', 'DISK_FULL', 'DISK_UNAVAILABLE', 'UNKNOWN')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE containers SET termination_reason = 'UNKNOWN' "
        "WHERE termination_reason = 'DISK_UNAVAILABLE'"
    )
    op.drop_constraint("ck_containers_termination_reason", "containers", type_="check")
    op.create_check_constraint(
        "ck_containers_termination_reason",
        "containers",
        "termination_reason IN ('TTL', 'USER', 'SCHEDULER', 'PREEMPTED', 'ADMIN', 'UNFUNDED', "
        "'MEMORY_EVICTED', 'DISK_FULL', 'UNKNOWN')",
    )
