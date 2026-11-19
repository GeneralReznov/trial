"""add shared multiplayer debate and discussion rooms

Revision ID: 82f4b6a1c9d3
Revises: 4c7e8a1d2f90
Create Date: 2026-10-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "82f4b6a1c9d3"
down_revision: Union[str, Sequence[str], None] = "4c7e8a1d2f90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "multiplayer_room",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("host_user_id", sa.Integer(), nullable=False),
        sa.Column("invite_code_hash", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=24), nullable=False),
        sa.Column("visibility", sa.String(length=12), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("topic", sa.String(length=500), nullable=False),
        sa.Column(
            "debate_format",
            sa.String(length=32),
            nullable=False,
            server_default="british_parliamentary",
        ),
        sa.Column("difficulty", sa.String(length=16), nullable=False, server_default="intermediate"),
        sa.Column("language", sa.String(length=16), nullable=False, server_default="en"),
        sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ai_side", sa.String(length=16), nullable=True),
        sa.Column("max_participants", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="in_progress"),
        sa.Column("state", JSONType, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "mode in ('debate','group_discussion')",
            name="ck_multiplayer_room_mode",
        ),
        sa.CheckConstraint(
            "visibility in ('public','private')",
            name="ck_multiplayer_room_visibility",
        ),
        sa.CheckConstraint(
            "status in ('in_progress','completed','cancelled')",
            name="ck_multiplayer_room_status",
        ),
        sa.CheckConstraint(
            "max_participants >= 2 and max_participants <= 24",
            name="ck_multiplayer_room_capacity",
        ),
        sa.ForeignKeyConstraint(["host_user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("public_id", name="uq_multiplayer_room_public_id"),
        sa.UniqueConstraint("invite_code_hash", name="uq_multiplayer_room_invite_hash"),
    )
    op.create_index("ix_multiplayer_room_public_id", "multiplayer_room", ["public_id"])
    op.create_index(
        "ix_multiplayer_room_host_user_id", "multiplayer_room", ["host_user_id"]
    )
    op.create_index(
        "ix_multiplayer_room_visibility_status",
        "multiplayer_room",
        ["visibility", "status"],
    )
    op.create_index(
        "ix_multiplayer_room_host_created",
        "multiplayer_room",
        ["host_user_id", "created_at"],
    )

    op.create_table(
        "multiplayer_room_member",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("is_host", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["room_id"], ["multiplayer_room.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "room_id", "user_id", name="uq_multiplayer_room_member_user"
        ),
    )
    op.create_index(
        "ix_multiplayer_room_member_room_id",
        "multiplayer_room_member",
        ["room_id"],
    )
    op.create_index(
        "ix_multiplayer_room_member_user_id",
        "multiplayer_room_member",
        ["user_id"],
    )
    op.create_index(
        "ix_multiplayer_room_member_room_active",
        "multiplayer_room_member",
        ["room_id", "is_active"],
    )
    op.create_index(
        "ix_multiplayer_room_member_user",
        "multiplayer_room_member",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_multiplayer_room_member_user", table_name="multiplayer_room_member")
    op.drop_index("ix_multiplayer_room_member_room_active", table_name="multiplayer_room_member")
    op.drop_index("ix_multiplayer_room_member_user_id", table_name="multiplayer_room_member")
    op.drop_index("ix_multiplayer_room_member_room_id", table_name="multiplayer_room_member")
    op.drop_table("multiplayer_room_member")
    op.drop_index("ix_multiplayer_room_host_created", table_name="multiplayer_room")
    op.drop_index("ix_multiplayer_room_visibility_status", table_name="multiplayer_room")
    op.drop_index("ix_multiplayer_room_host_user_id", table_name="multiplayer_room")
    op.drop_index("ix_multiplayer_room_public_id", table_name="multiplayer_room")
    op.drop_table("multiplayer_room")