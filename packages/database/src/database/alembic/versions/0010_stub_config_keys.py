"""Drop the retired placement keys from stored stub configurations.

A stub's configuration is validated against a model that refuses unknown
keys, and every stub written before revision 0009 carries `pool_selector`
and `pool` at its top level. Placement now lives in its own column.
"""

from alembic import op

revision = "0010_stub_config_keys"
down_revision = "0009_named_machines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE stubs SET configuration = configuration - 'pool_selector' - 'pool' "
        "WHERE configuration ? 'pool_selector' OR configuration ? 'pool'"
    )


def downgrade() -> None:
    pass
