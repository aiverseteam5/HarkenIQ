"""E0.1: the approval ledger, so a configured policy actually binds.

`cc_approval_policies` has carried `approval_mode`, `required_approvers`
and a group link since R2b, and nothing consulted any of them when a
decision was made. A tenant could configure dual authorization and get
single authorization, silently, because `cc_approval_routes` has room for
exactly one decision.

  cc_approval_records                 one row per approver per subject
  cc_approval_group_members.principal_ref   match membership on the
                                            Keycloak subject, not only
                                            on an address that changes
  cc_approval_policies.created_by     widened 32 -> 255, and the same on
  cc_approval_groups.created_by       groups: a Keycloak subject is a
                                      36-character UUID, so creating
                                      either raised
                                      StringDataRightTruncation on
                                      PostgreSQL and worked only on the
                                      sqlite used in tests

The unique constraint on (subject_type, subject_ref, approver_ref) is
what makes duplicate-approver prevention a database guarantee rather
than a check a later code path can forget.

Behaviour is unchanged on upgrade: with no policy configured the
required count is 1, so one approval decides exactly as it does today.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so a fresh database is born
    # with this table and column; only pre-E0 databases need the DDL.
    # Idempotence is mandatory for every additive migration in this chain.
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("cc_approval_records"):
        op.create_table(
            "cc_approval_records",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("subject_type", sa.String(24), nullable=False),
            sa.Column("subject_ref", sa.String(64), nullable=False),
            sa.Column("policy_id", sa.String(32), server_default=""),
            sa.Column("approver_ref", sa.String(128), nullable=False),
            sa.Column("approver_email", sa.String(320), server_default=""),
            sa.Column("decision", sa.String(16), nullable=False),
            sa.Column("scope_ok", sa.Boolean(), server_default=sa.true()),
            sa.Column("reason", sa.String(512), server_default=""),
            sa.Column("decided_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "subject_type", "subject_ref", "approver_ref",
                name="uq_approval_record_subject_approver",
            ),
        )
        op.create_index(
            "ix_cc_approval_records_tenant_id", "cc_approval_records", ["tenant_id"]
        )
        op.create_index(
            "ix_approval_records_subject",
            "cc_approval_records",
            ["subject_type", "subject_ref"],
        )

    columns = {
        c["name"] for c in inspector.get_columns("cc_approval_group_members")
    }
    if "principal_ref" not in columns:
        op.add_column(
            "cc_approval_group_members",
            sa.Column("principal_ref", sa.String(128), server_default=""),
        )

    # `created_by` was String(32) while a Keycloak subject is a 36-character
    # UUID, so creating an approval policy or group raised
    # StringDataRightTruncation on PostgreSQL and succeeded only on the
    # sqlite used in tests. Found on the live stack while proving E0.1.
    # sqlite ignores VARCHAR length, so this ALTER is postgres-only.
    if op.get_bind().dialect.name == "postgresql":
        for table in ("cc_approval_policies", "cc_approval_groups"):
            op.alter_column(
                table, "created_by",
                type_=sa.String(255), existing_type=sa.String(32),
                existing_nullable=True,
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in ("cc_approval_policies", "cc_approval_groups"):
            op.alter_column(
                table, "created_by",
                type_=sa.String(32), existing_type=sa.String(255),
                existing_nullable=True,
            )
    op.drop_column("cc_approval_group_members", "principal_ref")
    op.drop_index("ix_approval_records_subject", "cc_approval_records")
    op.drop_index("ix_cc_approval_records_tenant_id", "cc_approval_records")
    op.drop_table("cc_approval_records")
