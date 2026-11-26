"""Add users, dashboards, encrypted provider setup, and projections.

Revision ID: 0009_multitenant_auth_setup
Revises: 0008_tariff_rate_range_index
"""
from collections.abc import Sequence
from alembic import op

revision: str = "0009_multitenant_auth_setup"
down_revision: str | None = "0008_tariff_rate_range_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("""CREATE TABLE users (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), email text NOT NULL UNIQUE,
      google_subject text NOT NULL UNIQUE, display_name text, is_admin boolean NOT NULL DEFAULT false,
      created_at timestamptz NOT NULL DEFAULT now(), last_login_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE dashboard_accounts (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_user_id uuid NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
      display_name text NOT NULL DEFAULT 'Home energy', timezone text NOT NULL DEFAULT 'Europe/London',
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE dashboard_settings (
      dashboard_id uuid PRIMARY KEY REFERENCES dashboard_accounts(id) ON DELETE CASCADE,
      setup_complete boolean NOT NULL DEFAULT false, system_start_at timestamptz,
      earnings_config jsonb NOT NULL DEFAULT '{}'::jsonb, updated_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE provider_credentials (
      dashboard_id uuid NOT NULL REFERENCES dashboard_accounts(id) ON DELETE CASCADE,
      provider text NOT NULL CHECK (provider IN ('octopus','ecoflow_developer','ecoflow_app')),
      ciphertext bytea NOT NULL, nonce bytea NOT NULL, key_version integer NOT NULL DEFAULT 1,
      updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (dashboard_id, provider))""")
    op.execute("""CREATE TABLE projection_targets (
      dashboard_id uuid NOT NULL REFERENCES dashboard_accounts(id) ON DELETE CASCADE, fiscal_year integer NOT NULL,
      generation_kwh numeric(20,3) NOT NULL CHECK (generation_kwh > 0), solar_gbp numeric(16,2) NOT NULL CHECK (solar_gbp >= 0),
      battery_gbp numeric(16,2) NOT NULL CHECK (battery_gbp >= 0), export_gbp numeric(16,2) NOT NULL CHECK (export_gbp >= 0),
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (dashboard_id, fiscal_year))""")
    op.execute("""CREATE TABLE user_sessions (
      token_hash text PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      csrf_token text NOT NULL, expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE invitations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), email text NOT NULL UNIQUE, token_hash text NOT NULL UNIQUE,
      invited_by uuid REFERENCES users(id) ON DELETE SET NULL, expires_at timestamptz NOT NULL, consumed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now())""")

def downgrade() -> None:
    for table in ('invitations','user_sessions','projection_targets','provider_credentials','dashboard_settings','dashboard_accounts','users'):
        op.execute(f'DROP TABLE IF EXISTS {table} CASCADE')
