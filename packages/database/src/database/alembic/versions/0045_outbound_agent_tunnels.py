"""Retire inbound network state while preserving machine enrollment authority."""

from alembic import op

revision = "0045_outbound_agent_tunnels"
down_revision = "0044_stub_preparation_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE compute_machine_enrollments
        SET payload = payload - ARRAY[
            'network_generation', 'network_phase', 'network_peer_id', 'network_public_key',
            'network_address', 'network_verified_at', 'network_failure_detail'
        ]::text[]
        WHERE payload ?| ARRAY[
            'network_generation', 'network_phase', 'network_peer_id', 'network_public_key',
            'network_address', 'network_verified_at', 'network_failure_detail'
        ]::text[]
        """
    )
    op.execute(
        """
        UPDATE compute_units
        SET payload = (payload - 'transport') #- '{config,transport}'
        WHERE payload ? 'transport' OR payload->'config' ? 'transport'
        """
    )
    op.drop_column("compute_units", "transport")
    op.drop_table("wireguard_peers")
    op.drop_table("wireguard_gateway")


def downgrade() -> None:
    raise RuntimeError("retired network credentials cannot be reconstructed")
