"""record safe live-telemetry transport provenance

Revision ID: 0005_powerocean_live
Revises: 0004_octopus_mini_telemetry
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_powerocean_live"
down_revision = "0004_octopus_mini_telemetry"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("live_provider_health", sa.Column("transport", sa.String(length=32), nullable=True))
    op.create_check_constraint("ck_live_provider_health_transport", "live_provider_health", "transport IS NULL OR transport IN ('mqtt', 'http_fallback')")

def downgrade():
    op.drop_constraint("ck_live_provider_health_transport", "live_provider_health", type_="check")
    op.drop_column("live_provider_health", "transport")
