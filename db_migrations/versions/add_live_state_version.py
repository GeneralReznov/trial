"""add live state version

Revision ID: 4c7e8a1d2f90
Revises: 9f0a2d7c4b11
Create Date: 2026-09-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4c7e8a1d2f90"
down_revision: Union[str, Sequence[str], None] = "9f0a2d7c4b11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "practice_session",
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("practice_session", "state_version")