"""Database model layer for PrepSage AI.

Schema-only module. No queries, no UI. All models share a single declarative
Base so that migrations discover them when this module is imported by app.py.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    MappedAsDataclass,
    mapped_column,
    relationship,
)


class Base(MappedAsDataclass, DeclarativeBase, kw_only=True):
    """Single declarative base for the whole application."""


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


TimestampTZ = DateTime(timezone=True)
JSONType = JSON().with_variant(JSONB, "postgresql")

# --- Enumerated string values (kept as bounded strings + CHECK constraints) ---

ROLES = ("student", "admin")
PRACTICE_MODES = ("interview", "group_discussion", "debate", "coaching")
SESSION_STATUSES = (
    "created",
    "in_progress",
    "paused",
    "completed",
    "abandoned",
    "failed",
)
GOAL_STATUSES = ("active", "achieved", "paused", "archived")
TASK_STATUSES = ("pending", "in_progress", "done", "skipped")
DIFFICULTIES = ("beginner", "intermediate", "advanced", "expert")


class TimestampMixin(MappedAsDataclass):
    """created_at / updated_at columns maintained by the database."""

    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ,
        init=False,
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ,
        init=False,
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )


# --------------------------------------------------------------------------- #
# Users & authentication
# --------------------------------------------------------------------------- #


class User(TimestampMixin, Base):
    """Application user. Email is stored normalized (trimmed + lowercased)."""

    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("email", name="uq_app_user_email"),
        CheckConstraint("role in ('student','admin')", name="ck_app_user_role"),
        Index("ix_app_user_role_active", "role", "is_active"),
        Index("ix_app_user_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)

    # Identity
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(
        String(160), nullable=False, default=""
    )

    # PBKDF2 password hash metadata
    password_hash: Mapped[str] = mapped_column(
        String(255), nullable=False, default=""
    )
    password_salt: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    password_algorithm: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pbkdf2_sha256"
    )
    password_iterations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=390000
    )
    password_updated_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )

    # Access control / status
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="student"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    locked_until: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    last_login_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    last_login_ip: Mapped[str | None] = mapped_column(
        String(45), nullable=True, default=None
    )

    # Profile fields
    avatar_seed: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    headline: Mapped[str] = mapped_column(
        String(160), nullable=False, default=""
    )
    bio: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    institution: Mapped[str] = mapped_column(
        String(160), nullable=False, default=""
    )
    field_of_study: Mapped[str] = mapped_column(
        String(120), nullable=False, default=""
    )
    graduation_year: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    phone: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC"
    )
    locale: Mapped[str] = mapped_column(
        String(16), nullable=False, default="en"
    )

    # Onboarding
    onboarding_completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    onboarding_step: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    onboarding_completed_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    target_role: Mapped[str] = mapped_column(
        String(120), nullable=False, default=""
    )
    experience_level: Mapped[str] = mapped_column(
        String(32), nullable=False, default=""
    )
    preferred_modes: Mapped[list | None] = mapped_column(
        JSONType, nullable=True, default=None
    )
    weekly_practice_target: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3
    )
    preferences: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True, default=None
    )

    # Progression carried over from the original practice experience.
    level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="beginner"
    )
    total_points: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    practice_streak: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_practice_date: Mapped[dt.date | None] = mapped_column(
        Date, nullable=True, default=None
    )

    # Relationships (cascade-safe: children removed with the parent)
    sessions: Mapped[list["LoginSession"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        init=False,
        repr=False,
    )
    goals: Mapped[list["PreparationGoal"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        init=False,
        repr=False,
    )
    tasks: Mapped[list["LearnerTask"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        init=False,
        repr=False,
    )
    practice_sessions: Mapped[list["PracticeSession"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        init=False,
        repr=False,
    )
    documents: Mapped[list["UploadedDocument"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        init=False,
        repr=False,
    )
    achievements: Mapped[list["Achievement"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        init=False,
        repr=False,
    )


class LoginSession(Base):
    """Revocable persistent login session keyed by a hashed opaque token."""

    __tablename__ = "login_session"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_login_session_token_hash"),
        Index("ix_login_session_user_revoked", "user_id", "revoked_at"),
        Index("ix_login_session_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # sha256 hex of the opaque token; the raw token is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_prefix: Mapped[str] = mapped_column(
        String(12), nullable=False, default=""
    )

    expires_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, default=_utcnow
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ,
        init=False,
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    revoked_reason: Mapped[str | None] = mapped_column(
        String(120), nullable=True, default=None
    )

    ip_address: Mapped[str | None] = mapped_column(
        String(45), nullable=True, default=None
    )
    user_agent: Mapped[str | None] = mapped_column(
        String(512), nullable=True, default=None
    )

    user: Mapped["User"] = relationship(
        back_populates="sessions", init=False, repr=False
    )


# --------------------------------------------------------------------------- #
# Goals & tasks
# --------------------------------------------------------------------------- #


class PreparationGoal(TimestampMixin, Base):
    __tablename__ = "preparation_goal"
    __table_args__ = (
        CheckConstraint(
            "status in ('active','achieved','paused','archived')",
            name="ck_preparation_goal_status",
        ),
        Index("ix_preparation_goal_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )
    mode: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default=None
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active"
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    target_score: Mapped[float | None] = mapped_column(
        Numeric(5, 2), nullable=True, default=None
    )
    target_sessions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    completed_sessions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    progress_percent: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, default=0
    )
    due_date: Mapped[dt.date | None] = mapped_column(
        default=None, nullable=True
    )
    achieved_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )

    user: Mapped["User"] = relationship(
        back_populates="goals", init=False, repr=False
    )
    tasks: Mapped[list["LearnerTask"]] = relationship(
        back_populates="goal",
        cascade="save-update, merge",
        passive_deletes=True,
        init=False,
        repr=False,
    )


class LearnerTask(TimestampMixin, Base):
    __tablename__ = "learner_task"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending','in_progress','done','skipped')",
            name="ck_learner_task_status",
        ),
        Index("ix_learner_task_user_status_due", "user_id", "status", "due_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    goal_id: Mapped[int | None] = mapped_column(
        ForeignKey("preparation_goal.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    details: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )
    mode: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default=None
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    estimated_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=15
    )
    due_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    is_recommended: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    user: Mapped["User"] = relationship(
        back_populates="tasks", init=False, repr=False
    )
    goal: Mapped["PreparationGoal | None"] = relationship(
        back_populates="tasks", init=False, repr=False
    )


# --------------------------------------------------------------------------- #
# Preparation content
# --------------------------------------------------------------------------- #


class PreparationContent(TimestampMixin, Base):
    """Reusable question / prompt bank across all practice modes."""

    __tablename__ = "preparation_content"
    __table_args__ = (
        CheckConstraint(
            "mode in ('interview','group_discussion','debate','coaching')",
            name="ck_preparation_content_mode",
        ),
        CheckConstraint(
            "difficulty in ('beginner','intermediate','advanced','expert')",
            name="ck_preparation_content_difficulty",
        ),
        UniqueConstraint(
            "mode", "slug", name="uq_preparation_content_mode_slug"
        ),
        Index("ix_preparation_content_mode_active", "mode", "is_active"),
        Index("ix_preparation_content_mode_category", "mode", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)

    mode: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(180), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    guidance: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )
    ideal_answer: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )

    category: Mapped[str] = mapped_column(
        String(120), nullable=False, default="general"
    )
    topic: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    difficulty: Mapped[str] = mapped_column(
        String(16), nullable=False, default="intermediate"
    )
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="en"
    )
    expected_duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=120
    )
    max_score: Mapped[float] = mapped_column(
        Numeric(6, 2), nullable=False, default=10
    )

    tags: Mapped[list | None] = mapped_column(
        JSONType, nullable=True, default=None
    )
    rubric: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True, default=None
    )
    extra_config: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True, default=None
    )

    source: Mapped[str] = mapped_column(
        String(64), nullable=False, default="seed"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )

    responses: Mapped[list["SessionResponse"]] = relationship(
        back_populates="content",
        cascade="save-update, merge",
        passive_deletes=True,
        init=False,
        repr=False,
    )


# --------------------------------------------------------------------------- #
# Practice sessions & responses
# --------------------------------------------------------------------------- #


class PracticeSession(TimestampMixin, Base):
    __tablename__ = "practice_session"
    __table_args__ = (
        CheckConstraint(
            "mode in ('interview','group_discussion','debate','coaching')",
            name="ck_practice_session_mode",
        ),
        CheckConstraint(
            "status in ('created','in_progress','paused','completed','abandoned','failed')",
            name="ck_practice_session_status",
        ),
        UniqueConstraint("public_id", name="uq_practice_session_public_id"),
        Index("ix_practice_session_user_created", "user_id", "created_at"),
        Index("ix_practice_session_user_status", "user_id", "status"),
        Index("ix_practice_session_mode_status", "mode", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)
    public_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    goal_id: Mapped[int | None] = mapped_column(
        ForeignKey("preparation_goal.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("uploaded_document.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )

    mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="interview"
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="created"
    )

    # Configuration / setup form values
    role_target: Mapped[str] = mapped_column(
        String(160), nullable=False, default=""
    )
    topic: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    difficulty: Mapped[str] = mapped_column(
        String(16), nullable=False, default="intermediate"
    )
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="en"
    )
    question_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5
    )
    time_limit_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    config: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True, default=None
    )
    question_plan: Mapped[list | None] = mapped_column(
        JSONType, nullable=True, default=None
    )

    # Resumability
    current_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    answered_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    is_resumable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    legal_hold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    resume_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None
    )
    checkpoint: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True, default=None
    )

    # Scoring
    total_score: Mapped[float] = mapped_column(
        Numeric(7, 2), nullable=False, default=0
    )
    max_score: Mapped[float] = mapped_column(
        Numeric(7, 2), nullable=False, default=0
    )
    percentage_score: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, default=0
    )
    readiness_score: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, default=0
    )
    rubric_breakdown: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True, default=None
    )
    strengths: Mapped[list | None] = mapped_column(
        JSONType, nullable=True, default=None
    )
    improvements: Mapped[list | None] = mapped_column(
        JSONType, nullable=True, default=None
    )
    summary_feedback: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )
    transcript: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )

    # Timing
    started_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    last_activity_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # Error / reliability fields
    error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Optimistic concurrency guard for live-room read/AI/write updates.
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None
    )
    model_name: Mapped[str | None] = mapped_column(
        String(120), nullable=True, default=None
    )
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    user: Mapped["User"] = relationship(
        back_populates="practice_sessions", init=False, repr=False
    )
    responses: Mapped[list["SessionResponse"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SessionResponse.question_index",
        init=False,
        repr=False,
    )


class MultiplayerRoom(TimestampMixin, Base):
    """A shared, authenticated debate or group-discussion room."""

    __tablename__ = "multiplayer_room"
    __table_args__ = (
        CheckConstraint(
            "mode in ('debate','group_discussion')",
            name="ck_multiplayer_room_mode",
        ),
        CheckConstraint(
            "visibility in ('public','private')",
            name="ck_multiplayer_room_visibility",
        ),
        CheckConstraint(
            "status in ('in_progress','completed','cancelled')",
            name="ck_multiplayer_room_status",
        ),
        CheckConstraint(
            "max_participants >= 2 and max_participants <= 24",
            name="ck_multiplayer_room_capacity",
        ),
        UniqueConstraint("public_id", name="uq_multiplayer_room_public_id"),
        UniqueConstraint("invite_code_hash", name="uq_multiplayer_room_invite_hash"),
        Index("ix_multiplayer_room_visibility_status", "visibility", "status"),
        Index("ix_multiplayer_room_host_created", "host_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    host_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    visibility: Mapped[str] = mapped_column(String(12), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    topic: Mapped[str] = mapped_column(String(500), nullable=False)
    invite_code_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None
    )
    debate_format: Mapped[str] = mapped_column(
        String(32), nullable=False, default="british_parliamentary"
    )
    difficulty: Mapped[str] = mapped_column(
        String(16), nullable=False, default="intermediate"
    )
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    ai_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ai_side: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    max_participants: Mapped[int] = mapped_column(
        Integer, nullable=False, default=12
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="in_progress"
    )
    state: Mapped[dict | None] = mapped_column(
        JSONType, nullable=False, default_factory=dict
    )
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    last_activity_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )


class MultiplayerRoomMember(TimestampMixin, Base):
    """A user's membership and current debate position in a shared room."""

    __tablename__ = "multiplayer_room_member"
    __table_args__ = (
        UniqueConstraint(
            "room_id", "user_id", name="uq_multiplayer_room_member_user"
        ),
        Index("ix_multiplayer_room_member_room_active", "room_id", "is_active"),
        Index("ix_multiplayer_room_member_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)
    room_id: Mapped[int] = mapped_column(
        ForeignKey("multiplayer_room.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    is_host: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    joined_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, default=_utcnow
    )
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, nullable=False, default=_utcnow
    )


class SessionResponse(TimestampMixin, Base):
    """One learner answer within a practice session."""

    __tablename__ = "session_response"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "question_index", name="uq_session_response_position"
        ),
        Index("ix_session_response_session", "session_id", "question_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("practice_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content_id: Mapped[int | None] = mapped_column(
        ForeignKey("preparation_content.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        index=True,
    )

    question_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    question_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    answer_text: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )
    answer_audio_filename: Mapped[str | None] = mapped_column(
        String(255), nullable=True, default=None
    )
    answer_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="text"
    )
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    score: Mapped[float] = mapped_column(
        Numeric(6, 2), nullable=False, default=0
    )
    max_score: Mapped[float] = mapped_column(
        Numeric(6, 2), nullable=False, default=10
    )
    rubric_scores: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True, default=None
    )
    feedback: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )
    strengths: Mapped[list | None] = mapped_column(
        JSONType, nullable=True, default=None
    )
    improvements: Mapped[list | None] = mapped_column(
        JSONType, nullable=True, default=None
    )
    is_skipped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    is_graded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    started_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    answered_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )
    response_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )

    session: Mapped["PracticeSession"] = relationship(
        back_populates="responses", init=False, repr=False
    )
    content: Mapped["PreparationContent | None"] = relationship(
        back_populates="responses", init=False, repr=False
    )


# --------------------------------------------------------------------------- #
# Uploaded documents
# --------------------------------------------------------------------------- #


class UploadedDocument(TimestampMixin, Base):
    """Metadata for learner uploads (resumes, notes). Files live on disk."""

    __tablename__ = "uploaded_document"
    __table_args__ = (
        UniqueConstraint(
            "stored_filename", name="uq_uploaded_document_stored_filename"
        ),
        Index("ix_uploaded_document_user_created", "user_id", "created_at"),
        CheckConstraint("size_bytes >= 0", name="ck_uploaded_document_size"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    document_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="resume"
    )
    content_type: Mapped[str] = mapped_column(
        String(128), nullable=False, default=""
    )
    extension: Mapped[str] = mapped_column(
        String(16), nullable=False, default=""
    )
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checksum_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None
    )
    extracted_text: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_scanned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    scan_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    legal_hold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(
        TimestampTZ, nullable=True, default=None
    )

    user: Mapped["User"] = relationship(
        back_populates="documents", init=False, repr=False
    )


# --------------------------------------------------------------------------- #
# Audit log (append-only)
# --------------------------------------------------------------------------- #


class AuditRecord(Base):
    """Append-only audit trail, subject to configured retention and legal holds."""

    __tablename__ = "audit_record"
    __table_args__ = (
        Index("ix_audit_record_actor_created", "actor_id", "created_at"),
        Index("ix_audit_record_entity", "entity_type", "entity_id"),
        Index("ix_audit_record_action_created", "action", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)

    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )
    actor_email: Mapped[str] = mapped_column(
        String(320), nullable=False, default=""
    )
    actor_role: Mapped[str] = mapped_column(
        String(16), nullable=False, default=""
    )

    action: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    entity_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    entity_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )
    outcome: Mapped[str] = mapped_column(
        String(16), nullable=False, default="success"
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="info"
    )
    legal_hold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )

    # Request metadata
    ip_address: Mapped[str | None] = mapped_column(
        String(45), nullable=True, default=None
    )
    user_agent: Mapped[str | None] = mapped_column(
        String(512), nullable=True, default=None
    )
    request_path: Mapped[str | None] = mapped_column(
        String(512), nullable=True, default=None
    )
    request_method: Mapped[str | None] = mapped_column(
        String(12), nullable=True, default=None
    )
    session_token_prefix: Mapped[str | None] = mapped_column(
        String(12), nullable=True, default=None
    )
    metadata_json: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True, default=None
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ,
        init=False,
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        index=True,
    )


# --------------------------------------------------------------------------- #
# Learner achievements
# --------------------------------------------------------------------------- #


ACHIEVEMENT_DEFINITIONS = (
    {"key": "first_debate", "name": "First Debate", "icon": "⚡", "description": "Complete your first live debate."},
    {"key": "first_interview", "name": "First Interview", "icon": "◌", "description": "Complete your first interview rehearsal."},
    {"key": "first_gd", "name": "GD Rookie", "icon": "◎", "description": "Complete your first group discussion."},
    {"key": "sessions_5", "name": "Five Sessions", "icon": "✦", "description": "Complete five practice sessions."},
    {"key": "sessions_10", "name": "Practice Veteran", "icon": "◆", "description": "Complete ten practice sessions."},
    {"key": "debate_master", "name": "Debate Master", "icon": "♜", "description": "Score 80 or higher in a debate."},
    {"key": "interview_pro", "name": "Interview Pro", "icon": "▣", "description": "Score 80 or higher in an interview."},
    {"key": "gd_leader", "name": "GD Leader", "icon": "♟", "description": "Score 80 or higher for group-discussion leadership."},
    {"key": "multilingual", "name": "Multilingual", "icon": "◈", "description": "Complete practice in a non-English language."},
    {"key": "streak_7", "name": "Seven-Day Streak", "icon": "⌁", "description": "Practise seven days in a row."},
    {"key": "score_above_8", "name": "High Achiever", "icon": "★", "description": "Score 80 or higher in any session."},
    {"key": "perfect_debate", "name": "Perfect Debate", "icon": "◇", "description": "Score 90 or higher in a debate."},
)


class Achievement(Base):
    """A durable, user-owned achievement unlocked by practice milestones."""

    __tablename__ = "achievement"
    __table_args__ = (
        UniqueConstraint("user_id", "achievement_key", name="uq_achievement_user_key"),
        Index("ix_achievement_user_earned", "user_id", "earned_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    achievement_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    icon: Mapped[str] = mapped_column(String(8), nullable=False, default="★")
    description: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    earned_at: Mapped[dt.datetime] = mapped_column(
        TimestampTZ, init=False, nullable=False, default=_utcnow, server_default=func.now()
    )
    user: Mapped["User"] = relationship(
        back_populates="achievements", init=False, repr=False
    )


__all__ = [
    "Base",
    "User",
    "LoginSession",
    "PreparationGoal",
    "LearnerTask",
    "PreparationContent",
    "PracticeSession",
    "MultiplayerRoom",
    "MultiplayerRoomMember",
    "SessionResponse",
    "UploadedDocument",
    "AuditRecord",
    "Achievement",
    "ACHIEVEMENT_DEFINITIONS",
    "ROLES",
    "PRACTICE_MODES",
    "SESSION_STATUSES",
    "GOAL_STATUSES",
    "TASK_STATUSES",
    "DIFFICULTIES",
]
