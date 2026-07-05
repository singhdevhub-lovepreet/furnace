"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("plan", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "github_installations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_login", sa.Text(), nullable=False),
    )
    op.create_table(
        "repos",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "installation_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("github_installations.id"),
            nullable=False,
        ),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=False),
    )
    op.create_table(
        "llm_keys",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("enc_key", sa.LargeBinary(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("repo_id", sa.Uuid(as_uuid=True), sa.ForeignKey("repos.id"), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("model_policy", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "session_id", sa.Uuid(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=False
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "session_id", sa.Uuid(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=False
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
    )
    op.create_table(
        "usage_records",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "session_id", sa.Uuid(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=False
        ),
        sa.Column("mac_seconds", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=False),
        sa.Column("mac_cost_usd", sa.Numeric(12, 4), nullable=False),
        sa.UniqueConstraint("session_id"),
    )
    op.create_table(
        "mac_hosts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("ext_host_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("allocated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("min_release_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "mac_sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("host_id", sa.Uuid(as_uuid=True), sa.ForeignKey("mac_hosts.id"), nullable=False),
        sa.Column("vm_name", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("mac_sessions")
    op.drop_table("mac_hosts")
    op.drop_table("usage_records")
    op.drop_table("artifacts")
    op.drop_table("events")
    op.drop_table("sessions")
    op.drop_table("llm_keys")
    op.drop_table("repos")
    op.drop_table("github_installations")
    op.drop_table("users")
