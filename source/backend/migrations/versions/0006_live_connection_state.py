"""Persist safe MQTT connection state for live providers."""
from alembic import op
import sqlalchemy as sa

revision = "0006_live_connection_state"
down_revision = "0005_powerocean_live"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("live_provider_health", sa.Column("connection_state", sa.String(length=32), nullable=True))
    op.create_check_constraint("ck_live_provider_health_connection_state", "live_provider_health", "connection_state IS NULL OR connection_state IN ('connecting', 'connected', 'stale', 'disconnected')")

def downgrade():
    op.drop_constraint("ck_live_provider_health_connection_state", "live_provider_health", type_="check")
    op.drop_column("live_provider_health", "connection_state")
