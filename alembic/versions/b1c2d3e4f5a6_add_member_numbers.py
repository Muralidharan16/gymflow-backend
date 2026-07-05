"""add_member_numbers

Revision ID: b1c2d3e4f5a6
Revises: fe1543f281fc
Create Date: 2026-06-13 20:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "fe1543f281fc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("members", sa.Column("member_number", sa.Integer(), nullable=True))

    op.execute(
        """
        WITH numbered AS (
            SELECT
                id,
                org_id,
                row_number() OVER (
                    PARTITION BY org_id
                    ORDER BY created_at, id
                ) + 99 AS assigned_number
            FROM members
            WHERE org_id IS NOT NULL
        )
        UPDATE members m
        SET member_number = numbered.assigned_number
        FROM numbered
        WHERE m.id = numbered.id
          AND m.member_number IS NULL;
        """
    )

    op.execute(
        """
        INSERT INTO organization_counters (id, org_id, counter_key, current_value)
        SELECT
            gen_random_uuid(),
            org_id,
            'member',
            max(member_number)
        FROM members
        WHERE org_id IS NOT NULL
          AND member_number IS NOT NULL
        GROUP BY org_id
        ON CONFLICT (org_id, counter_key)
        DO UPDATE SET
            current_value = GREATEST(
                organization_counters.current_value,
                EXCLUDED.current_value
            ),
            updated_at = now();
        """
    )

    op.alter_column("members", "member_number", nullable=False)
    op.create_unique_constraint(
        "uq_members_org_member_number",
        "members",
        ["org_id", "member_number"],
    )
    op.create_index(
        "ix_members_org_branch_status",
        "members",
        ["org_id", "home_branch_id", "status"],
        unique=False,
    )
    op.create_index("ix_members_org_phone", "members", ["org_id", "phone"], unique=False)
    op.execute("CREATE INDEX ix_members_org_name_lower ON members (org_id, lower(name));")
    op.execute("CREATE INDEX ix_members_name_trgm ON members USING gin (lower(name) gin_trgm_ops);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_members_name_trgm;")
    op.execute("DROP INDEX IF EXISTS ix_members_org_name_lower;")
    op.drop_index("ix_members_org_phone", table_name="members")
    op.drop_index("ix_members_org_branch_status", table_name="members")
    op.drop_constraint("uq_members_org_member_number", "members", type_="unique")
    op.drop_column("members", "member_number")
    op.execute("DELETE FROM organization_counters WHERE counter_key = 'member';")
