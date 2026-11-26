"""Persist announced Axle VPP events for the dashboard timeline."""
from alembic import op

revision = "0017_axle_vpp_events"
down_revision = "0016_dashboard_selection"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE vpp_events (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          dashboard_id uuid NOT NULL REFERENCES dashboard_accounts(id) ON DELETE CASCADE,
          provider varchar(32) NOT NULL,
          start_at timestamptz NOT NULL,
          end_at timestamptz NOT NULL,
          direction varchar(16) NOT NULL,
          provider_updated_at timestamptz,
          last_seen_at timestamptz NOT NULL DEFAULT now(),
          created_at timestamptz NOT NULL DEFAULT now(),
          CHECK (end_at > start_at),
          CHECK (direction IN ('import', 'export', 'unknown')),
          UNIQUE (dashboard_id, provider, start_at, end_at)
        )
    """)
    op.execute("CREATE INDEX ix_vpp_events_dashboard_start ON vpp_events(dashboard_id, start_at DESC)")
    op.execute("ALTER TABLE vpp_events ENABLE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY vpp_events_dashboard_scope ON vpp_events
      USING (dashboard_id::text = current_setting('app.dashboard_id', true))
      WITH CHECK (dashboard_id::text = current_setting('app.dashboard_id', true))""")


def downgrade():
    op.execute("DROP TABLE vpp_events")
