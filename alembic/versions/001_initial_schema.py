"""Initial Doers schema

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-05-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB, TIMESTAMP

revision: str = "001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ENUM_TYPES = [
    ("orgtier", ["basic", "pro", "elite"]),
    ("staffrole", ["owner", "admin", "trainer", "receptionist"]),
    ("memberstatus", ["active", "inactive", "frozen", "expired", "blocked"]),
    ("subscriptionstatus", ["active", "expired", "frozen", "cancelled", "pending"]),
    ("paymentmethod", ["cash", "upi", "card", "bank_transfer", "cheque", "online"]),
    ("paymenttype", ["subscription", "registration", "addon", "penalty", "refund"]),
    ("paymentstatus", ["pending", "completed", "failed", "refunded"]),
    ("checkinmethod", ["qr", "fingerprint", "manual", "rfid", "face", "door_lock"]),
    ("attendancedenialreason", ["subscription_expired", "no_active_subscription", "account_frozen", "not_found"]),
    ("freezestatus", ["requested", "active", "completed", "cancelled"]),
    ("invoicestatus", ["draft", "issued", "paid", "void"]),
    ("invoicetype", ["bill_of_supply", "tax_invoice"]),
    ("importstatus", ["processing", "completed", "failed"]),
]


def upgrade() -> None:
    # 0. Ensure uuid-ossp or pgcrypto for gen_random_uuid()
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    # Create enums
    for name, values in ENUM_TYPES:
        sa.Enum(*values, name=name).create(op.get_bind(), checkfirst=True)

    # 1. organizations
    op.create_table(
        "organizations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("pan_number", sa.String(10), nullable=False),
        sa.Column("tier", sa.Enum("basic", "pro", "elite", name="orgtier", create_type=False), nullable=False, server_default="basic"),
        sa.Column("facility_type", sa.String(30), nullable=False, server_default="gym"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_organizations_pan", "organizations", ["pan_number"], unique=True)

    # 2. gyms
    op.create_table(
        "gyms",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("gymu_id", sa.String(20), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("city", sa.String(100), nullable=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_gyms_org_id", "gyms", ["org_id"])
    op.create_index("ix_gyms_gymu_id", "gyms", ["gymu_id"], unique=True)

    # 3. branch_tax_settings
    op.create_table(
        "branch_tax_settings",
        sa.Column("gym_id", UUID(as_uuid=True), sa.ForeignKey("gyms.id", ondelete="CASCADE"), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("gst_number", sa.String(15), nullable=False),
        sa.Column("legal_name", sa.String(), nullable=False),
        sa.Column("gst_rate", sa.Numeric(5, 2), nullable=False, server_default="18.00"),
        sa.Column("sac_code", sa.String(10), nullable=False, server_default="996319"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_branch_tax_gst", "branch_tax_settings", ["gst_number"], unique=True)

    # 4. gym_owners (staff)
    op.create_table(
        "gym_owners",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gym_id", UUID(as_uuid=True), sa.ForeignKey("gyms.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("role", sa.Enum("owner", "admin", "trainer", "receptionist", name="staffrole", create_type=False), nullable=False, server_default="admin"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_login", TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("org_id", "email", name="uq_gym_owners_org_email"),
    )
    op.create_index("ix_gym_owners_org_id", "gym_owners", ["org_id"])

    # 5. members
    op.create_table(
        "members",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("gym_id", UUID(as_uuid=True), sa.ForeignKey("gyms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_uid", sa.String(20), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("gender", sa.String(10), nullable=True),
        sa.Column("blood_group", sa.String(5), nullable=True),
        sa.Column("emergency_contact_name", sa.String(), nullable=True),
        sa.Column("emergency_contact_phone", sa.String(), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("height_cm", sa.Numeric(5, 2), nullable=True),
        sa.Column("weight_kg", sa.Numeric(5, 2), nullable=True),
        sa.Column("fingerprint_id", sa.String(), nullable=True),
        sa.Column("photo_url", sa.String(), nullable=True),
        sa.Column("qr_token", sa.String(), nullable=True),
        sa.Column("status", sa.Enum("active", "inactive", "frozen", "expired", "blocked", name="memberstatus", create_type=False), nullable=False, server_default="active"),
        sa.Column("source", sa.String(30), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_migrated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("migrated_source", sa.String(50), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("gym_owners.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_by", UUID(as_uuid=True), sa.ForeignKey("gym_owners.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_members_phone", "members", ["phone"])
    op.create_index("ix_members_gym_phone", "members", ["gym_id", "phone"])
    op.create_index("ix_members_email", "members", ["email"])
    op.create_index("ix_members_member_uid", "members", ["member_uid"], unique=True)
    op.create_index("ix_members_gym_id", "members", ["gym_id"])
    op.create_index("ix_members_org_id", "members", ["org_id"])
    op.create_index("ix_members_qr_token", "members", ["qr_token"], unique=True)
    op.create_index("ix_members_status", "members", ["status"])

    # 6. member_measurements
    op.create_table(
        "member_measurements",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("gym_id", UUID(as_uuid=True), sa.ForeignKey("gyms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_id", UUID(as_uuid=True), sa.ForeignKey("members.id", ondelete="CASCADE"), nullable=False),
        sa.Column("measured_on", sa.Date(), nullable=False),
        sa.Column("weight_kg", sa.Numeric(5, 2), nullable=True),
        sa.Column("height_cm", sa.Numeric(5, 2), nullable=True),
        sa.Column("body_fat_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recorded_by", UUID(as_uuid=True), sa.ForeignKey("gym_owners.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_measurements_member_date", "member_measurements", ["member_id", sa.text("measured_on DESC")])

    # 7. subscription_plans
    op.create_table(
        "subscription_plans",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("gym_id", UUID(as_uuid=True), sa.ForeignKey("gyms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("max_freeze_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("features", JSONB, nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_subscription_plans_gym_id", "subscription_plans", ["gym_id"])

    # 8. member_subscriptions
    op.create_table(
        "member_subscriptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("gym_id", UUID(as_uuid=True), sa.ForeignKey("gyms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_id", UUID(as_uuid=True), sa.ForeignKey("members.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", UUID(as_uuid=True), sa.ForeignKey("subscription_plans.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("gym_owners.id", ondelete="SET NULL"), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("freeze_start_date", sa.Date(), nullable=True),
        sa.Column("freeze_end_date", sa.Date(), nullable=True),
        sa.Column("total_freeze_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Enum("active", "expired", "frozen", "cancelled", "pending", name="subscriptionstatus", create_type=False), nullable=False, server_default="active"),
        sa.Column("reminder_sent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("mandate_id", UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("cancelled_at", TIMESTAMP(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_member_subs_gym_status", "member_subscriptions", ["gym_id", "status"])
    op.create_index("ix_member_subs_member_status", "member_subscriptions", ["member_id", "status"])
    op.create_index("ix_member_subs_end_date", "member_subscriptions", ["end_date"])
    op.create_index("ix_member_subs_gym_id", "member_subscriptions", ["gym_id"])

    # 9. payments
    op.create_table(
        "payments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("gym_id", UUID(as_uuid=True), sa.ForeignKey("gyms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_id", UUID(as_uuid=True), sa.ForeignKey("members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("subscription_id", UUID(as_uuid=True), sa.ForeignKey("member_subscriptions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("collected_by", UUID(as_uuid=True), sa.ForeignKey("gym_owners.id", ondelete="SET NULL"), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("discount_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("payment_method", sa.Enum("cash", "upi", "card", "bank_transfer", "cheque", "online", name="paymentmethod", create_type=False), nullable=False),
        sa.Column("payment_type", sa.Enum("subscription", "registration", "addon", "penalty", "refund", name="paymenttype", create_type=False), nullable=False),
        sa.Column("status", sa.Enum("pending", "completed", "failed", "refunded", name="paymentstatus", create_type=False), nullable=False),
        sa.Column("transaction_reference", sa.String(), nullable=True),
        sa.Column("razorpay_id", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("raw_payload", JSONB, nullable=True),
        sa.Column("is_migrated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("payment_date", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_payments_gym_id", "payments", ["gym_id"])
    op.create_index("ix_payments_member_id", "payments", ["member_id"])
    op.create_index("ix_payments_gym_date", "payments", ["gym_id", sa.text("payment_date DESC")])

    # 10. invoices
    op.create_table(
        "invoices",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("gym_id", UUID(as_uuid=True), sa.ForeignKey("gyms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_id", UUID(as_uuid=True), sa.ForeignKey("members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("payment_id", UUID(as_uuid=True), sa.ForeignKey("payments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("subscription_id", UUID(as_uuid=True), sa.ForeignKey("member_subscriptions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("invoice_number", sa.String(), nullable=False),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=False),
        sa.Column("discount_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("tax_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("tax_rate", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("invoice_type", sa.Enum("bill_of_supply", "tax_invoice", name="invoicetype", create_type=False), nullable=False, server_default="bill_of_supply"),
        sa.Column("status", sa.Enum("draft", "issued", "paid", "void", name="invoicestatus", create_type=False), nullable=False, server_default="issued"),
        sa.Column("pdf_url", sa.String(), nullable=True),
        sa.Column("issued_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_invoices_invoice_number", "invoices", ["invoice_number"], unique=True)
    op.create_index("ix_invoices_gym_id", "invoices", ["gym_id"])
    op.create_index("ix_invoices_member_id", "invoices", ["member_id"])

    # 11. attendance_logs
    op.create_table(
        "attendance_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("gym_id", UUID(as_uuid=True), sa.ForeignKey("gyms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_id", UUID(as_uuid=True), sa.ForeignKey("members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("scan_time", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("check_out_time", TIMESTAMP(timezone=True), nullable=True),
        sa.Column("check_in_method", sa.Enum("qr", "fingerprint", "manual", "rfid", "face", "door_lock", name="checkinmethod", create_type=False), nullable=False),
        sa.Column("access_granted", sa.Boolean(), nullable=False),
        sa.Column("denial_reason", sa.Enum("subscription_expired", "no_active_subscription", "account_frozen", "not_found", name="attendancedenialreason", create_type=False), nullable=True),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_attendance_gym_scan", "attendance_logs", ["gym_id", sa.text("scan_time DESC")])
    op.create_index("ix_attendance_gym_id", "attendance_logs", ["gym_id"])
    op.create_index("ix_attendance_member_id", "attendance_logs", ["member_id"])
    op.create_index("ix_attendance_scan_time", "attendance_logs", ["scan_time"])

    # 12. member_freeze_logs
    op.create_table(
        "member_freeze_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("gym_id", UUID(as_uuid=True), sa.ForeignKey("gyms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_id", UUID(as_uuid=True), sa.ForeignKey("members.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subscription_id", UUID(as_uuid=True), sa.ForeignKey("member_subscriptions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("gym_owners.id", ondelete="SET NULL"), nullable=True),
        sa.Column("freeze_start", sa.Date(), nullable=False),
        sa.Column("freeze_end", sa.Date(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.Enum("requested", "active", "completed", "cancelled", name="freezestatus", create_type=False), nullable=False, server_default="requested"),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_freeze_logs_member_sub", "member_freeze_logs", ["member_id", "subscription_id"])

    # 13. import_logs
    op.create_table(
        "import_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("gym_id", UUID(as_uuid=True), sa.ForeignKey("gyms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("imported_by", UUID(as_uuid=True), sa.ForeignKey("gym_owners.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("status", sa.Enum("processing", "completed", "failed", name="importstatus", create_type=False), nullable=False, server_default="processing"),
        sa.Column("total_rows", sa.Integer(), nullable=True),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors_payload", JSONB, nullable=False, server_default="[]"),
        sa.Column("column_mapping", JSONB, nullable=False, server_default="{}"),
        sa.Column("source_software", sa.String(50), nullable=True),
        sa.Column("completed_at", TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_import_logs_gym_id", "import_logs", ["gym_id"])

    # Data migration: fix old payment status
    op.execute("UPDATE payments SET status='completed' WHERE status='success'")


def downgrade() -> None:
    op.drop_table("import_logs")
    op.drop_table("member_freeze_logs")
    op.drop_table("attendance_logs")
    op.drop_table("invoices")
    op.drop_table("payments")
    op.drop_table("member_subscriptions")
    op.drop_table("subscription_plans")
    op.drop_table("member_measurements")
    op.drop_table("members")
    op.drop_table("gym_owners")
    op.drop_table("branch_tax_settings")
    op.drop_table("gyms")
    op.drop_table("organizations")

    for name, _ in reversed(ENUM_TYPES):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)