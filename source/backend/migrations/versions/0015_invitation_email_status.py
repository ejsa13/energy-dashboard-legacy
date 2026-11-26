"""Track invitation email submission status.

Revision ID: 0015_invitation_email_status
Revises: 0014_dashboard_members
"""
from alembic import op

revision = "0015_invitation_email_status"
down_revision = "0014_dashboard_members"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE invitations ADD COLUMN email_status text NOT NULL DEFAULT 'Unknown'")


def downgrade():
    op.execute("ALTER TABLE invitations DROP COLUMN email_status")
