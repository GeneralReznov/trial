"""add retention legal holds

Revision ID: 9f0a2d7c4b11
Revises: e66c75d2e6ae
Create Date: 2026-09-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9f0a2d7c4b11"
down_revision: Union[str, Sequence[str], None] = "e66c75d2e6ae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "practice_session",
        sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "uploaded_document",
        sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "audit_record",
        sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_practice_session_legal_hold", "practice_session", ["legal_hold"], unique=False
    )
    op.create_index(
        "ix_uploaded_document_legal_hold", "uploaded_document", ["legal_hold"], unique=False
    )
    op.create_index(
        "ix_audit_record_legal_hold", "audit_record", ["legal_hold"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_audit_record_legal_hold", table_name="audit_record")
    op.drop_index("ix_uploaded_document_legal_hold", table_name="uploaded_document")
    op.drop_index("ix_practice_session_legal_hold", table_name="practice_session")
    op.drop_column("audit_record", "legal_hold")
    op.drop_column("uploaded_document", "legal_hold")
    op.drop_column("practice_session", "legal_hold")