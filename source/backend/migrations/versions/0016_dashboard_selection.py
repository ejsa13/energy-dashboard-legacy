"""Scope invitations and remember each session's selected dashboard."""
from alembic import op
revision = "0016_dashboard_selection"
down_revision = "0015_invitation_email_status"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("ALTER TABLE invitations DROP CONSTRAINT invitations_email_key")
    op.execute("ALTER TABLE invitations ADD CONSTRAINT invitations_email_dashboard_key UNIQUE NULLS NOT DISTINCT (email,dashboard_id)")
    op.execute("ALTER TABLE user_sessions ADD COLUMN selected_dashboard_id uuid REFERENCES dashboard_accounts(id) ON DELETE SET NULL")

def downgrade():
    op.execute("ALTER TABLE user_sessions DROP COLUMN selected_dashboard_id")
    op.execute("ALTER TABLE invitations DROP CONSTRAINT invitations_email_dashboard_key")
    op.execute("ALTER TABLE invitations ADD CONSTRAINT invitations_email_key UNIQUE(email)")
