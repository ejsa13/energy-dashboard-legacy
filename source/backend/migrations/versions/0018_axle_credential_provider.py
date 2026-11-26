"""Allow the encrypted Axle VPP credential provider."""
from alembic import op

revision = "0018_axle_credential_provider"
down_revision = "0017_axle_vpp_events"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE provider_credentials DROP CONSTRAINT provider_credentials_provider_check")
    op.execute("""ALTER TABLE provider_credentials ADD CONSTRAINT provider_credentials_provider_check
      CHECK (provider IN ('octopus','ecoflow_developer','ecoflow_app','axle_vpp'))""")


def downgrade():
    op.execute("DELETE FROM provider_credentials WHERE provider='axle_vpp'")
    op.execute("ALTER TABLE provider_credentials DROP CONSTRAINT provider_credentials_provider_check")
    op.execute("""ALTER TABLE provider_credentials ADD CONSTRAINT provider_credentials_provider_check
      CHECK (provider IN ('octopus','ecoflow_developer','ecoflow_app'))""")
