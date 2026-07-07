"""add auth fields and unique user emails

Revision ID: 0002_auth_multitenancy
Revises: 0001_initial
Create Date: 2026-07-06 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_auth_multitenancy"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=True))
    op.execute(
        sa.text("UPDATE users SET password_hash = 'disabled-no-login' WHERE password_hash IS NULL")
    )
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("password_hash", existing_type=sa.Text(), nullable=False)
        batch_op.create_unique_constraint("uq_users_email", ["email"])


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("uq_users_email", type_="unique")
        batch_op.drop_column("password_hash")
