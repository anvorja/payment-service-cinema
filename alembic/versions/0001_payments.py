"""payments: historial de cobros por Wompi (boletas y recargas Cinema+)

Revision ID: 0001_payments
Revises:
Create Date: 2026-09-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_payments"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("reference", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("user_email", sa.String(255), nullable=False),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("amount_in_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("transaction_id", sa.String(64), nullable=True),
        sa.Column("payment_method_type", sa.String(40), nullable=True),
        sa.Column("last_four", sa.String(4), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("order_context", sa.JSON(), nullable=True),
        sa.Column("refund_status", sa.String(20), nullable=True),
        sa.Column("refund_detail", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("reference", name="uq_payments_reference"),
        sa.UniqueConstraint("transaction_id", name="uq_payments_transaction_id"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'declined', 'voided', 'error')", name="ck_payments_status"
        ),
        sa.CheckConstraint("kind IN ('tickets', 'recharge')", name="ck_payments_kind"),
        sa.CheckConstraint(
            "refund_status IS NULL OR refund_status IN ('voided', 'manual_required')",
            name="ck_payments_refund_status",
        ),
        sa.CheckConstraint("amount_in_cents > 0", name="ck_payments_amount"),
    )
    op.create_index("ix_payments_user_email", "payments", ["user_email"])
    op.create_index(
        "ux_payments_order_id", "payments", ["order_id"], unique=True,
        postgresql_where=sa.text("order_id IS NOT NULL"),
    )
    op.create_index(
        "ix_payments_pending", "payments", ["status", "expires_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("payments")
