"""Store the collector's last successful database heartbeat."""
from alembic import op
import sqlalchemy as sa

revision = "0001_collector_heartbeat"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "collector_heartbeat",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("collector_heartbeat")
