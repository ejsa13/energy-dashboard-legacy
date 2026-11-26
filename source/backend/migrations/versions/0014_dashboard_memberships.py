"""Share configured dashboards with invited users.

Revision ID: 0014_dashboard_members
Revises: 0013_tariff_interval_idx
"""
from alembic import op

revision = "0014_dashboard_members"
down_revision = "0013_tariff_interval_idx"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("""CREATE TABLE dashboard_memberships (
      dashboard_id uuid NOT NULL REFERENCES dashboard_accounts(id) ON DELETE CASCADE,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (dashboard_id, user_id))""")
    op.execute("INSERT INTO dashboard_memberships(dashboard_id,user_id) SELECT id,owner_user_id FROM dashboard_accounts ON CONFLICT DO NOTHING")
    op.execute("ALTER TABLE invitations ADD COLUMN dashboard_id uuid REFERENCES dashboard_accounts(id) ON DELETE CASCADE")
    op.execute("CREATE INDEX ix_invitations_dashboard_email ON invitations(dashboard_id,email)")

def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_invitations_dashboard_email")
    op.execute("ALTER TABLE invitations DROP COLUMN IF EXISTS dashboard_id")
    op.execute("DROP TABLE IF EXISTS dashboard_memberships")
