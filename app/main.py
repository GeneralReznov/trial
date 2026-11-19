"""PrepSage AI FastAPI application.

One ASGI service delivers the browser client, JSON API, authentication,
document handling, practice workflows, reports, and the operations console.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import csv
import io
import json
import logging
import logging.config
import os
import re
import secrets
import sys
import zipfile
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, ConfigDict, EmailStr, Field, StrictBool, StrictInt, StrictStr, field_validator
from sqlalchemy import case, create_engine, delete, func, inspect, or_, select, text, update, event as sa_event
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.utils import security
from app.services.ai_features import (
    LANGUAGES,
    FeatureProviderError,
    coach_reply,
    context_guidance,
    debate_evaluation,
    debate_turn,
    detect_language,
    gd_evaluation,
    gd_responses,
    interview_turn,
    practice_evaluation,
    practice_plan,
    practice_summary,
    provider_status,
    transcribe_audio,
)
from app.services.speech_service import synthesize_text
from app.services.provider_observability import (
    provider_snapshot,
    record_provider_event,
    user_safe_provider_status,
)
from app.services.gemini_service import (
    ProviderError,
    evaluate_answer,
    generate_question_plan,
    is_configured,
    model_name,
    summarise_session,
)
from app.models import (
    AuditRecord,
    ACHIEVEMENT_DEFINITIONS,
    Achievement,
    Base,
    DIFFICULTIES,
    LearnerTask,
    LoginSession,
    MultiplayerRoom,
    MultiplayerRoomMember,
    PracticeSession,
    PreparationContent,
    PreparationGoal,
    SessionResponse,
    UploadedDocument,
    User,
)
from app.utils.wudc_scale import WUDC_SCALE, wudc_score, wudc_tier
from app.request_limits import RequestBodyLimitMiddleware

# --------------------------------------------------------------------------- #
# O1: Structured JSON logging
# --------------------------------------------------------------------------- #

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "logging.Formatter",
            "fmt": '{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","msg":%(message)s}',
            "datefmt": "%Y-%m-%dT%H:%M:%SZ",
        },
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json" if os.getenv("LOG_FORMAT", "json") == "json" else "plain",
            "stream": "ext://sys.stdout",
        }
    },
    "root": {"level": _LOG_LEVEL, "handlers": ["console"]},
})
logger = logging.getLogger(__name__)

_application_loop: asyncio.AbstractEventLoop | None = None


def _await_on_application_loop(coroutine: Any) -> Any:
    """Run provider coroutines from a synchronous FastAPI worker thread."""
    loop = _application_loop
    if loop is None or not loop.is_running():
        if hasattr(coroutine, "close"):
            coroutine.close()
        raise RuntimeError("The application event loop is not available.")
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is loop:
        if hasattr(coroutine, "close"):
            coroutine.close()
        raise RuntimeError("Provider coroutines must be bridged from a worker thread.")
    return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

# --------------------------------------------------------------------------- #
# Database / paths
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("PREPSAGE_DATA_DIR", str(ROOT / "data"))).expanduser()
UPLOAD_DIR = DATA_DIR / "uploads"
# Prefer an explicitly selected external database. Replit reserves
# DATABASE_URL for its managed database, while Render and Vercel commonly
# provide DATABASE_URL themselves.
DATABASE_URL = (
    os.getenv("PREPSAGE_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or f"sqlite:///{DATA_DIR / 'prepsage.db'}"
)

# O2: Fail fast when PostgreSQL is explicitly required but unreachable at boot.
_REQUIRE_PG = os.getenv("REQUIRE_POSTGRES", "").lower() in {"1", "true", "yes"}

# Normalise PostgreSQL driver to psycopg v3.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql+psycopg2://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)

_IS_SQLITE = DATABASE_URL.startswith("sqlite")

_connect_args: dict[str, Any] = {}
if _IS_SQLITE:
    _connect_args["check_same_thread"] = False

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
)

# D1/H10: Enable foreign-key enforcement for SQLite connections.
if _IS_SQLITE:
    @sa_event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn: Any, _: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# Retention is deliberately explicit and configurable. A deployment can choose
# shorter windows for privacy or longer windows for its approved records policy.
def _retention_days(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using default %s.", name, raw, default)
        return default
    return max(1, min(value, 36500))


_RETENTION = {
    "login_sessions": _retention_days("PREPSAGE_LOGIN_SESSION_RETENTION_DAYS", 30),
    "audit_records": _retention_days("PREPSAGE_AUDIT_RETENTION_DAYS", 3650),
    "practice_sessions": _retention_days("PREPSAGE_PRACTICE_RETENTION_DAYS", 730),
    "documents": _retention_days("PREPSAGE_DOCUMENT_RETENTION_DAYS", 365),
}
_MIGRATION_ADVISORY_LOCK_KEY = 731_946_205_118_401
_RETENTION_ADVISORY_LOCK_KEY = 731_946_205_118_402
try:
    _configured_cleanup_interval = int(
        os.getenv("PREPSAGE_CLEANUP_INTERVAL_SECONDS", "3600")
    )
except (TypeError, ValueError):
    _configured_cleanup_interval = 3600
_CLEANUP_INTERVAL_SECONDS = max(60, min(_configured_cleanup_interval, 86400))
_cleanup_metrics: dict[str, Any] = {
    "last_run_at": None,
    "last_success": None,
    "last_error": None,
    "deleted": {"login_sessions": 0, "audit_records": 0, "practice_sessions": 0, "documents": 0},
}

# --------------------------------------------------------------------------- #
# H3: In-process rate limiter (per IP, per endpoint)
# --------------------------------------------------------------------------- #

import threading
_rate_lock = threading.Lock()
_rate_buckets: dict[str, list[float]] = defaultdict(list)

_RATE_LIMITS: dict[str, tuple[int, int]] = {
    # endpoint-key: (max_requests, window_seconds)
    "/api/auth/login":    (10, 60),
    "/api/auth/register": (5,  60),
    "default":            (120, 60),
}

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")

def _rate_limit(request: Request) -> None:
    import time
    ip = _client_ip(request)
    path = request.url.path
    limit, window = _RATE_LIMITS.get(path, _RATE_LIMITS["default"])
    key = f"{ip}:{path}"
    now_ts = time.monotonic()
    with _rate_lock:
        hits = _rate_buckets[key]
        # Evict timestamps outside the window
        hits[:] = [t for t in hits if now_ts - t < window]
        if len(hits) >= limit:
            raise HTTPException(429, "Too many requests. Please slow down.")
        hits.append(now_ts)


# --------------------------------------------------------------------------- #
# A1-A2: Pydantic request schemas (typed contracts, no raw dict payloads)
# --------------------------------------------------------------------------- #

class RegisterRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=2, max_length=160)
    password: str = Field(..., min_length=10, max_length=128)
    confirm_password: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., max_length=128)

class OnboardingRequest(BaseModel):
    target_role: str = Field("", max_length=120)
    experience_level: str = Field("", max_length=32)
    target_date: str = Field("", max_length=16)
    weekly_target: int = Field(3, ge=1, le=14)
    weekly_minutes: int = Field(60, ge=15, le=600)
    focus_note: str = Field("", max_length=500)
    preferred_modes: list[str] = Field(default_factory=list)

    @field_validator("target_date", mode="before")
    @classmethod
    def validate_target_date(cls, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("Use a valid target date in YYYY-MM-DD format.")
        normalized = value.strip()
        if not normalized:
            return ""
        try:
            return dt.date.fromisoformat(normalized).isoformat()
        except ValueError as exc:
            raise ValueError("Use a valid target date in YYYY-MM-DD format.") from exc

class GoalRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=240)
    mode: str = Field("", max_length=32)
    status: str = Field("active", max_length=16)
    target_sessions: int = Field(5, ge=0, le=10000)
    target_score: float = Field(0, ge=0, le=100)
    due_date: str = Field("", max_length=16)
    description: str = Field("", max_length=1000)
    priority: int = Field(2, ge=1, le=3)

class TaskRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=240)
    mode: str = Field("", max_length=32)
    goal_id: int = Field(0, ge=0)
    due_date: str = Field("", max_length=16)
    estimated_minutes: int = Field(30, ge=5, le=480)
    details: str = Field("", max_length=1000)
    priority: int = Field(2, ge=1, le=3)

class TaskStatusRequest(BaseModel):
    status: str = Field(..., max_length=16)

class PracticeStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["interview", "group_discussion", "debate", "coaching"] = "interview"
    role_target: str = Field("", max_length=160)
    company: str = Field("", max_length=160)
    interview_type: Literal["mixed", "behavioral", "technical", "case"] = "mixed"
    interview_minutes: int = Field(5, ge=1, le=10)
    difficulty: Literal["beginner", "intermediate", "advanced", "expert"] = "intermediate"
    language: str = Field("en", max_length=8)
    question_count: int = Field(5, ge=1, le=10)
    rounds: int = Field(3, ge=1, le=10)
    context_notes: str = Field("", max_length=2000)
    project_info: str = Field("", max_length=2000)
    document_id: int = Field(0, ge=0)
    interviewer_id: Literal["lisa", "bob"] = Field(
        "lisa",
        validation_alias=AliasChoices("interviewer_id", "interviewer"),
    )
    topic: str = Field("", max_length=200)
    motion: str = Field("", max_length=200)
    side: Literal["proposition", "opposition"] | None = None
    debate_format: Literal[
        "british_parliamentary",
        "public_forum",
        "oxford_style",
        "asian_parliamentary",
        "worlds_schools",
    ] | None = None
    participants: str = Field("", max_length=240)
    focus_area: str = Field("", max_length=500)

class AnswerRequest(BaseModel):
    answer: str = Field("", max_length=10000)
    source: str = Field("text", max_length=16)
    response_seconds: int = Field(0, ge=0, le=86400)
    skip: bool = False
    tab_switch_count: int = Field(0, ge=0, le=1000)

class SessionStatusRequest(BaseModel):
    status: str = Field(..., max_length=16)
    tab_switch_count: int = Field(0, ge=0, le=1000)


class StrictRequest(BaseModel):
    """Base contract for public JSON mutations.

    Rejecting unknown fields is intentional: it keeps frontend and backend
    contracts aligned instead of silently dropping or coercing input.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GoalStatusRequest(StrictRequest):
    status: Literal["active", "achieved", "paused", "archived"]


class CoachHistoryItem(StrictRequest):
    role: Literal["user", "assistant"]
    content: StrictStr = Field(..., min_length=1, max_length=4000)


class CoachRequest(StrictRequest):
    message: StrictStr = Field(..., min_length=1, max_length=6000)
    history: list[CoachHistoryItem] = Field(default_factory=list, max_length=20)
    mode: StrictStr = Field("coach", min_length=1, max_length=32)
    language: StrictStr = Field("en", min_length=2, max_length=8)
    user_goal: StrictStr = Field("", max_length=500)


class ContextGuidanceRequest(StrictRequest):
    context_notes: StrictStr = Field(..., min_length=8, max_length=2000)
    mode: Literal["interview", "group_discussion", "debate", "coaching"] = "interview"
    role_target: StrictStr = Field("", max_length=500)
    topic: StrictStr = Field("", max_length=500)
    focus_area: StrictStr = Field("", max_length=500)
    company: StrictStr = Field("", max_length=500)
    difficulty: StrictStr = Field("", max_length=32)
    language: StrictStr = Field("en", min_length=2, max_length=8)
    variation: StrictInt = Field(0, ge=0, le=2_147_483_647)
    previous_guidance: StrictStr = Field("", max_length=700)


class SpeechSynthesisRequest(StrictRequest):
    text: StrictStr = Field(..., min_length=1, max_length=5000)
    language: StrictStr = Field("en", min_length=2, max_length=8)
    voice_profile: Literal["lisa", "bob"] = "lisa"


class AccountUpdateRequest(StrictRequest):
    full_name: StrictStr = Field(..., min_length=2, max_length=160)
    headline: StrictStr = Field("", max_length=160)
    institution: StrictStr = Field("", max_length=160)
    field_of_study: StrictStr = Field("", max_length=120)
    target_role: StrictStr = Field("", max_length=120)
    timezone: StrictStr = Field("UTC", max_length=64)
    phone: StrictStr | None = Field(default=None, max_length=32)


class PasswordChangeRequest(StrictRequest):
    current_password: StrictStr = Field(..., min_length=1, max_length=128)
    new_password: StrictStr = Field(..., min_length=10, max_length=128)
    confirm_new_password: StrictStr = Field(..., min_length=10, max_length=128)


class InterviewTurnRequest(StrictRequest):
    answer: StrictStr = Field(..., min_length=12, max_length=12000)
    response_seconds: StrictInt = Field(0, ge=0, le=86_400)
    source: StrictStr = Field("text", min_length=1, max_length=16)
    tab_switch_count: StrictInt = Field(0, ge=0, le=1000)


class DebateStartRequest(StrictRequest):
    motion: StrictStr = Field("", max_length=500)
    topic: StrictStr = Field("", max_length=500)
    side: Literal["proposition", "opposition"] = "proposition"
    format: Literal[
        "british_parliamentary",
        "public_forum",
        "oxford_style",
        "asian_parliamentary",
        "worlds_schools",
    ] = "british_parliamentary"
    difficulty: Literal["beginner", "intermediate", "advanced", "expert"] = "intermediate"
    language: StrictStr = Field("en", min_length=2, max_length=8)


class DebateSpeechRequest(StrictRequest):
    public_id: StrictStr = Field("", max_length=36)
    session_id: StrictStr = Field("", max_length=36)
    speech: StrictStr = Field("", max_length=7000)
    answer: StrictStr = Field("", max_length=7000)
    speech_type: Literal["constructive", "rebuttal", "summary"] = "constructive"
    type: Literal["constructive", "rebuttal", "summary"] | None = None


class DebatePoiRequest(StrictRequest):
    public_id: StrictStr = Field("", max_length=36)
    session_id: StrictStr = Field("", max_length=36)
    poi: StrictStr = Field(..., min_length=4, max_length=1500)


class LiveRoomRequest(StrictRequest):
    public_id: StrictStr = Field("", max_length=36)
    session_id: StrictStr = Field("", max_length=36)


class GroupDiscussionStartRequest(StrictRequest):
    topic: StrictStr = Field(..., min_length=4, max_length=500)
    participants: StrictStr | list[StrictStr] = Field(default="")
    difficulty: Literal["beginner", "intermediate", "advanced", "expert"] = "intermediate"
    language: StrictStr = Field("en", min_length=2, max_length=8)


class GroupDiscussionMessageRequest(StrictRequest):
    public_id: StrictStr = Field(..., min_length=1, max_length=36)
    message: StrictStr = Field(..., min_length=10, max_length=5000)


class MultiplayerRoomCreateRequest(StrictRequest):
    mode: Literal["debate", "group_discussion"]
    visibility: Literal["public", "private"] = "public"
    topic: StrictStr = Field(..., min_length=4, max_length=500)
    title: StrictStr = Field("", max_length=160)
    side: Literal["proposition", "opposition"] = "proposition"
    debate_format: Literal[
        "british_parliamentary",
        "public_forum",
        "oxford_style",
        "asian_parliamentary",
        "worlds_schools",
    ] = "british_parliamentary"
    difficulty: Literal["beginner", "intermediate", "advanced", "expert"] = "intermediate"
    language: StrictStr = Field("en", min_length=2, max_length=8)
    ai_enabled: StrictBool = False
    ai_side: Literal["proposition", "opposition"] | None = None
    max_participants: StrictInt = Field(12, ge=2, le=24)


class MultiplayerRoomJoinRequest(StrictRequest):
    public_id: StrictStr = Field("", max_length=36)
    invite_code: StrictStr = Field("", max_length=32)
    side: Literal["proposition", "opposition"] | None = None
    display_name: StrictStr = Field("", max_length=80)


class MultiplayerRoomTurnRequest(StrictRequest):
    request_id: StrictStr = Field(..., min_length=8, max_length=64)
    message: StrictStr = Field(..., min_length=4, max_length=5000)
    speech_type: Literal["constructive", "rebuttal", "summary"] = "constructive"


class AdminLearnerUpdateRequest(StrictRequest):
    is_active: StrictBool | None = None
    role: Literal["student", "admin"] | None = None


class AdminContentRequest(StrictRequest):
    mode: Literal["interview", "group_discussion", "debate", "coaching"] = "interview"
    title: StrictStr = Field(..., min_length=3, max_length=240)
    prompt: StrictStr = Field(..., min_length=10, max_length=8000)
    guidance: StrictStr = Field("", max_length=4000)
    category: StrictStr = Field("general", max_length=120)
    topic: StrictStr = Field("", max_length=160)
    difficulty: Literal["beginner", "intermediate", "advanced", "expert"] = "intermediate"
    duration_seconds: StrictInt = Field(120, ge=30, le=3600)
    tags: list[StrictStr] = Field(default_factory=list, max_length=20)
    rubric: dict[str, StrictStr | StrictInt | float] = Field(default_factory=dict, max_length=30)


class AdminContentToggleRequest(StrictRequest):
    is_active: StrictBool


class LegacyDebateStartRequest(StrictRequest):
    motion: StrictStr = Field("", max_length=500)
    topic: StrictStr = Field("", max_length=500)
    side: Literal["proposition", "opposition"] | None = None
    position: StrictStr = Field("", max_length=32)
    format: StrictStr = Field("british_parliamentary", max_length=40)
    difficulty: StrictStr = Field("intermediate", max_length=16)
    language: StrictStr = Field("en", min_length=2, max_length=8)


class LegacyInterviewStartRequest(StrictRequest):
    role: StrictStr = Field("", max_length=160)
    role_target: StrictStr = Field("", max_length=160)
    company: StrictStr = Field("", max_length=160)
    interview_type: StrictStr = Field("mixed", max_length=32)
    difficulty: StrictStr = Field("intermediate", max_length=16)
    language: StrictStr = Field("en", min_length=2, max_length=8)
    total_questions: StrictInt | None = Field(default=None, ge=1, le=10)
    question_count: StrictInt | None = Field(default=None, ge=1, le=10)
    project_info: StrictStr = Field("", max_length=2000)


class LegacyInterviewAnswerRequest(StrictRequest):
    public_id: StrictStr = Field("", max_length=36)
    session_id: StrictStr = Field("", max_length=36)
    answer: StrictStr = Field("", max_length=10000)
    source: StrictStr = Field("text", max_length=16)
    response_seconds: StrictInt = Field(0, ge=0, le=86_400)


class LegacySessionRequest(StrictRequest):
    public_id: StrictStr = Field("", max_length=36)
    session_id: StrictStr = Field("", max_length=36)


class LegacyInterviewEndRequest(LegacySessionRequest):
    tab_switch_count: StrictInt = Field(0, ge=0, le=1000)
# --------------------------------------------------------------------------- #
# A3: Typed health / readiness endpoints
# --------------------------------------------------------------------------- #

class HealthResponse(BaseModel):
    status: str
    db: str
    version: str = "1.0.0"

# --------------------------------------------------------------------------- #
# H2: CSRF double-submit cookie middleware
# --------------------------------------------------------------------------- #

_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_CSRF_COOKIE = "prepsage_csrf"
_CSRF_HEADER = "x-prepsage-csrf"
_MAX_JSON_BODY_BYTES = 1 * 1024 * 1024
_MAX_MULTIPART_BODY_BYTES = 14 * 1024 * 1024
_BOOTSTRAP_ADMIN_TOKEN = os.getenv("PREPSAGE_BOOTSTRAP_ADMIN_TOKEN", "").strip()
_LEGACY_ROUTES = {
    "/debate/start", "/debate/speech", "/debate/poi", "/debate/end",
    "/gd/start", "/gd/message", "/gd/end",
    "/interview/start", "/interview/answer", "/interview/next", "/interview/end",
}
_LEGACY_SUNSET = "Tue, 30 Sep 2027 23:59:59 GMT"
_LEGACY_SUCCESSORS = {
    "/debate/start": "/api/live/debate/start",
    "/debate/speech": "/api/live/debate/speech",
    "/debate/poi": "/api/live/debate/poi",
    "/debate/end": "/api/live/debate/end",
    "/gd/start": "/api/live/gd/start",
    "/gd/message": "/api/live/gd/message",
    "/gd/end": "/api/live/gd/end",
    "/interview/start": "/api/practice",
    "/interview/answer": "/api/interview/{public_id}/turn",
    "/interview/next": "/api/practice/{public_id}",
    "/interview/end": "/api/practice/{public_id}",
}

def _csrf_token() -> str:
    return secrets.token_hex(32)

def _validate_csrf(request: Request) -> None:
    """Verify CSRF double-submit for all state-changing requests."""
    if request.method in _CSRF_SAFE_METHODS:
        return
    # Skip CSRF for the login/register endpoints (no session yet)
    if request.url.path in {"/api/auth/login", "/api/auth/register"}:
        return
    if (
        request.url.path == "/api/auth/bootstrap-admin"
        and request.headers.get("x-prepsage-bootstrap-token", "")
    ):
        return
    cookie_val = request.cookies.get(_CSRF_COOKIE, "")
    header_val = request.headers.get(_CSRF_HEADER, "")
    if not cookie_val or not secrets.compare_digest(cookie_val, header_val):
        raise HTTPException(403, "CSRF token missing or invalid.")

# --------------------------------------------------------------------------- #
# H10: Live session JSON size guard
# --------------------------------------------------------------------------- #

_MAX_LIVE_STATE_BYTES = 512 * 1024  # 512 KB

def _guard_live_state_size(state: dict[str, Any]) -> None:
    if len(json.dumps(state).encode()) > _MAX_LIVE_STATE_BYTES:
        raise HTTPException(413, "Live session state is too large. End the session and start a new one.")


def _commit_live_state(
    session: Session,
    row: PracticeSession,
    expected_version: int,
    state: dict[str, Any],
    *,
    status: str | None = None,
    fields: dict[str, Any] | None = None,
) -> PracticeSession:
    """Atomically persist a live-room mutation only if its read is current.

    AI work happens outside the database transaction. The version predicate
    makes the final write compare-and-swap: one concurrent request wins and a
    stale request receives a conflict instead of erasing the winner's state.
    """
    values: dict[str, Any] = {
        "config": {**(row.config or {}), "live_state": state},
        "state_version": PracticeSession.state_version + 1,
        "last_activity_at": now(),
    }
    if status is not None:
        values["status"] = status
    if fields:
        values.update(fields)
    criteria = [
        PracticeSession.id == row.id,
        PracticeSession.user_id == row.user_id,
        PracticeSession.state_version == expected_version,
        PracticeSession.status == "in_progress",
    ]
    result = session.execute(update(PracticeSession).where(*criteria).values(**values))
    if result.rowcount != 1:
        raise HTTPException(
            409,
            "This live room changed while your response was being prepared. Refresh and try again.",
        )
    updated = session.scalar(
        select(PracticeSession)
        .where(PracticeSession.id == row.id)
        .execution_options(populate_existing=True)
    )
    if not updated:
        raise HTTPException(409, "That live room is no longer available.")
    return updated


def _run_retention_cleanup() -> dict[str, int]:
    """Delete expired data while preserving records marked for legal hold."""
    cutoff = now()
    login_cutoff = cutoff - dt.timedelta(days=_RETENTION["login_sessions"])
    audit_cutoff = cutoff - dt.timedelta(days=_RETENTION["audit_records"])
    practice_cutoff = cutoff - dt.timedelta(days=_RETENTION["practice_sessions"])
    document_cutoff = cutoff - dt.timedelta(days=_RETENTION["documents"])
    stored_paths: list[Path] = []
    deleted = {
        "login_sessions": 0,
        "audit_records": 0,
        "practice_sessions": 0,
        "documents": 0,
    }

    with db() as session:
        if not _IS_SQLITE:
            acquired = session.scalar(
                select(func.pg_try_advisory_xact_lock(_RETENTION_ADVISORY_LOCK_KEY))
            )
            if not acquired:
                logger.debug("Retention cleanup skipped; another instance holds the cleanup lock.")
                return {}
        login_count = session.execute(
            delete(LoginSession).where(
                LoginSession.created_at < login_cutoff,
                LoginSession.expires_at < cutoff,
                or_(
                    LoginSession.revoked_at.is_(None),
                    LoginSession.revoked_at < login_cutoff,
                ),
            )
        )
        deleted["login_sessions"] = int(login_count.rowcount or 0)
        audit_count = session.execute(
            delete(AuditRecord).where(
                AuditRecord.created_at < audit_cutoff,
                AuditRecord.legal_hold.is_(False),
            )
        )
        deleted["audit_records"] = int(audit_count.rowcount or 0)

        old_practice = session.scalars(
            select(PracticeSession).where(
                PracticeSession.created_at < practice_cutoff,
                PracticeSession.status.in_(["completed", "abandoned", "failed"]),
                PracticeSession.legal_hold.is_(False),
            )
        ).all()
        for row in old_practice:
            session.delete(row)
        deleted["practice_sessions"] = len(old_practice)

        old_documents = session.scalars(
            select(UploadedDocument).where(
                UploadedDocument.created_at < document_cutoff,
                UploadedDocument.legal_hold.is_(False),
            )
        ).all()
        for row in old_documents:
            stored_paths.append(UPLOAD_DIR / row.stored_filename)
            session.delete(row)
        deleted["documents"] = len(old_documents)

    for path in stored_paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove retained document file %s.", path)
    _cleanup_metrics.update(
        last_run_at=cutoff.isoformat(),
        last_success=True,
        last_error=None,
        deleted=deleted,
    )
    logger.info("Retention cleanup completed: %s", deleted)
    return deleted


async def _retention_cleanup_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(_run_retention_cleanup)
        except Exception as exc:
            _cleanup_metrics.update(
                last_run_at=now().isoformat(),
                last_success=False,
                last_error=str(exc)[:500],
            )
            logger.exception("Retention cleanup failed.")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_CLEANUP_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue

# --------------------------------------------------------------------------- #
# H7-H9: File-upload magic-byte / MIME validation
# --------------------------------------------------------------------------- #

_MAGIC_SIGNATURES: dict[str, list[bytes]] = {
    ".pdf":  [b"%PDF"],
    ".docx": [b"PK\x03\x04"],
    ".txt":  [],            # plain text: no magic bytes, content check only
}

_DECLARED_MIME: dict[str, set[str]] = {
    ".pdf":  {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
    },
    ".txt":  {"text/plain", "text/plain; charset=utf-8", "application/octet-stream"},
}

def _validate_upload_magic(data: bytes, extension: str) -> None:
    """Reject files whose magic bytes disagree with the declared extension."""
    sigs = _MAGIC_SIGNATURES.get(extension, [])
    if not sigs:
        return
    if not any(data[:len(sig)] == sig for sig in sigs):
        raise HTTPException(422, "File contents do not match the declared file type.")


class DocumentExtractionError(Exception):
    """An expected document-parser failure with a safe user-facing message."""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _safe_docx_text(data: bytes) -> str:
    """Extract text from a DOCX, refusing ZIP archives with too many entries."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            # H8: Limit number of archive members to prevent zip-bomb expansion
            members = zf.infolist()
            if len(members) > 200:
                raise HTTPException(422, "The uploaded file has too many internal entries.")
            total_uncompressed = sum(m.file_size for m in members)
            if total_uncompressed > 50 * 1024 * 1024:
                raise HTTPException(422, "The uploaded file expands to more than 50 MB when decompressed.")
            try:
                xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
            except KeyError as exc:
                raise DocumentExtractionError(
                    "docx_document_xml_missing",
                    "We could not process this DOCX file because it has no document content.",
                ) from exc
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                raise DocumentExtractionError(
                    "docx_read_failed",
                    "We could not process this DOCX file. It may be corrupt or unsupported.",
                ) from exc
            return re.sub("<[^>]+>", "", xml).replace("</w:p>", "\n")
    except zipfile.BadZipFile as exc:
        raise DocumentExtractionError(
            "docx_archive_invalid",
            "We could not process this DOCX file. It may be corrupt or unsupported.",
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise DocumentExtractionError(
            "docx_read_failed",
            "We could not process this DOCX file. It may be corrupt or unsupported.",
        ) from exc


def _record_document_scan_failure(
    *,
    data: bytes,
    name: str,
    extension: str,
    content_type: str,
    user_id: int,
    status: str = "extraction_failed",
) -> None:
    """Keep a safe metadata record when extraction fails before normal storage."""
    stored = f"{secrets.token_hex(16)}{extension}"
    stored_path = UPLOAD_DIR / stored
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        stored_path.write_bytes(data)
        with db() as session:
            session.add(UploadedDocument(
                user_id=user_id,
                original_filename=re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:255],
                stored_filename=stored,
                document_type=extension.lstrip("."),
                content_type=content_type[:128],
                extension=extension,
                size_bytes=len(data),
                checksum_sha256=hashlib.sha256(data).hexdigest(),
                scan_status=status,
            ))
    except (OSError, SQLAlchemyError):
        logger.exception(
            "document_scan_status_persist_failed extension=%s user_id=%s status=%s",
            extension,
            user_id,
            status,
        )
        try:
            stored_path.unlink(missing_ok=True)
        except OSError:
            pass

# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #

def _ensure_progress_columns() -> None:
    """Add progression columns when upgrading an existing development DB.

    ``create_all`` intentionally does not alter tables that already exist.
    The project started with a richer account model, so keep old local
    databases usable without making users delete their data.
    """
    columns = {item["name"] for item in inspect(engine).get_columns("app_user")}
    additions = {
        "level": "VARCHAR(20) NOT NULL DEFAULT 'beginner'",
        "total_points": "INTEGER NOT NULL DEFAULT 0",
        "practice_streak": "INTEGER NOT NULL DEFAULT 0",
        "last_practice_date": "DATE",
    }
    missing = [(name, definition) for name, definition in additions.items() if name not in columns]
    if not missing:
        return
    with engine.begin() as connection:
        for name, definition in missing:
            connection.execute(text(f'ALTER TABLE app_user ADD COLUMN "{name}" {definition}'))


def _prepare_sqlite_storage() -> None:
    """Create only the directory required by a local SQLite database."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    database_path = DATABASE_URL.split("///", 1)[-1].split("?", 1)[0]
    if database_path in {":memory:", ""}:
        return
    Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _migration_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "db_migrations"))
    config.attributes["database_url"] = DATABASE_URL
    return config


# A4: Disable interactive docs in production
_DOCS_URL = "/api/docs" if os.getenv("PREPSAGE_ENV", "development") != "production" else None
_REDOC_URL = None

# O3/O4: Use lifespan context manager (replaces deprecated @app.on_event)
@asynccontextmanager
async def _lifespan(application: "FastAPI"):  # noqa: F821
    global _application_loop
    _application_loop = asyncio.get_running_loop()
    cleanup_stop: asyncio.Event | None = None
    cleanup_task: asyncio.Task[None] | None = None
    try:
        # Keep migration and connectivity work off the request event loop.
        await asyncio.to_thread(_run_database_migrations)
        if _REQUIRE_PG and _IS_SQLITE:
            logger.error('"REQUIRE_POSTGRES=true" set but DATABASE_URL is SQLite. Aborting.')
            sys.exit(1)
        if not _IS_SQLITE:
            try:
                await asyncio.to_thread(_check_database_connection)
            except Exception as exc:
                logger.error('"Cannot connect to PostgreSQL at startup: %s"', exc)
                if _REQUIRE_PG:
                    sys.exit(1)
        cleanup_stop = asyncio.Event()
        cleanup_task = asyncio.create_task(_retention_cleanup_loop(cleanup_stop))
        logger.info('"PrepSage AI started"')
        yield
    finally:
        if cleanup_stop and cleanup_task:
            cleanup_stop.set()
            # Allow an in-flight threadpool cleanup to finish before disposing
            # the shared engine.
            await cleanup_task
        engine.dispose()
        _application_loop = None
        logger.info('"PrepSage AI stopped"')


def _check_database_connection() -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


app = FastAPI(title="PrepSage AI", docs_url=_DOCS_URL, redoc_url=_REDOC_URL, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def request_security(request: Request, call_next: Any) -> Response:
    """Apply browser and legacy-route protections consistently.

    The double-submit cookie is deliberately enforced at the middleware
    boundary so legacy compatibility routes cannot accidentally bypass it.
    Login and registration remain exempt because they establish the session
    and CSRF cookie.
    """
    path = request.url.path
    protected_path = path.startswith("/api/") or path in _LEGACY_ROUTES
    if protected_path:
        try:
            _rate_limit(request)
            _validate_csrf(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(self), geolocation=(), payment=(), usb=(), microphone=(self)"
    )
    configured_dev_host = (
        os.getenv("REPLIT_DEV_DOMAIN", "")
        .strip()
        .lower()
        .removeprefix("https://")
        .removeprefix("http://")
        .split("/", 1)[0]
    )
    is_replit_preview = bool(configured_dev_host) and (
        request.url.hostname or ""
    ).lower() == configured_dev_host
    if is_replit_preview:
        # Replit Workspace Preview embeds the development app in its own
        # editor frame. Keep production embedding disabled below.
        if "X-Frame-Options" in response.headers:
            del response.headers["X-Frame-Options"]
        frame_ancestors = "'self' https://replit.com https://*.replit.com"
    else:
        response.headers["X-Frame-Options"] = "DENY"
        frame_ancestors = "'none'"
    content_security_policy = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; "
        f"frame-ancestors {frame_ancestors}; form-action 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob:; connect-src 'self'; frame-src 'none'"
    )
    # Keep a report-only copy while the policy is observed in deployment.
    # The application shell has no inline scripts, so enforce the same policy
    # there. Swagger UI is third-party generated HTML and remains report-only
    # until its CDN/inline bootstrap can be migrated to a nonce.
    response.headers["Content-Security-Policy-Report-Only"] = content_security_policy
    if path not in {"/api/docs", "/api/redoc"}:
        response.headers["Content-Security-Policy"] = content_security_policy
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    if request.url.scheme == "https" or forwarded_proto == "https" or _DOCS_URL is None:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    if path in _LEGACY_ROUTES:
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = _LEGACY_SUNSET
        response.headers["Link"] = f'<{_LEGACY_SUCCESSORS[path]}>; rel="successor-version"'
        response.headers["X-PrepSage-Compatibility"] = "legacy-v1"
    return response


app.add_middleware(
    RequestBodyLimitMiddleware,
    json_limit=_MAX_JSON_BODY_BYTES,
    multipart_limit=_MAX_MULTIPART_BODY_BYTES,
)


def _run_database_migrations() -> None:
    """Upgrade the configured database before serving requests.

    C3 fix: The legacy fallback (stamp + create_all) is only triggered when
    *both* conditions are true: the app_user table exists (proving an old DB)
    AND alembic_version is absent (proving it predates Alembic). Any other
    failure (bad credentials, network, permissions) re-raises immediately so
    operators see the real error instead of silently corrupting migrations.
    """
    _prepare_sqlite_storage()
    config = _migration_config()
    if _IS_SQLITE:
        _run_database_migrations_with_config(config)
        return

    with engine.connect() as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_key)"),
            {"lock_key": _MIGRATION_ADVISORY_LOCK_KEY},
        )
        connection.commit()
        config.attributes["connection"] = connection
        try:
            _run_database_migrations_with_config(config)
        finally:
            config.attributes.pop("connection", None)
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": _MIGRATION_ADVISORY_LOCK_KEY},
                )
                connection.commit()
            except Exception:
                # Closing a pooled connection does not guarantee the database
                # session is closed; invalidate it so a failed unlock cannot
                # return a lock-owning connection to the pool.
                connection.invalidate()
                logger.exception("Could not release the database migration lock.")


def _run_database_migrations_with_config(config: Config) -> None:
    try:
        command.upgrade(config, "head")
    except Exception as exc:
        tables = set(inspect(engine).get_table_names())
        known_legacy_tables = {
            "app_user", "login_session", "preparation_goal", "learner_task",
            "preparation_content", "practice_session", "session_response",
            "uploaded_document", "audit_record", "achievement",
        }
        # A lone app_user table is not enough evidence of a supported legacy
        # schema; it is also a common symptom of a partially failed migration.
        legacy = (
            "app_user" in tables
            and "alembic_version" not in tables
            and tables <= known_legacy_tables
            and len(tables & {"login_session", "practice_session", "preparation_goal"}) >= 1
        )
        if not legacy:
            logger.error('"Migration failed (not a legacy schema): %s"', exc)
            raise
        logger.warning('"Detected legacy unversioned database — stamping at baseline before migrating"')
        Base.metadata.create_all(engine)
        _ensure_progress_columns()
        command.stamp(config, "head")
        # Run any remaining migrations now that baseline is stamped.
        command.upgrade(config, "head")
    _ensure_progress_columns()

MODE_LABELS = {
    "interview": "Interview preparation",
    "group_discussion": "Group discussion",
    "debate": "Structured debate",
    "coaching": "Personal coaching",
}
STATUS_LABELS = {
    "created": "Setting up",
    "in_progress": "In progress",
    "paused": "Paused",
    "completed": "Completed",
    "abandoned": "Abandoned",
    "failed": "Needs another try",
}
RUBRICS = {
    "interview": ["Relevance", "Structure", "Evidence", "Communication", "Impact"],
    "group_discussion": ["Content depth", "Clarity", "Initiative", "Collaboration", "Summarising"],
    "debate": ["Matter", "Manner", "Method", "Role fulfilment", "Engagement with clash"],
    "coaching": ["Self-awareness", "Specificity", "Action orientation", "Reflection quality"],
}
INTERVIEWERS = {
    "lisa": {
        "name": "Explorer Lisa",
        "initials": "L",
        "description": "Warm, curious and precise. Lisa explores the story behind your answer.",
        "opening": "Hello, I’m Lisa. I’ll ask a few focused questions and one follow-up when it helps. Take your time and answer as you would in the room.",
    },
    "bob": {
        "name": "Empathetic Bob",
        "initials": "B",
        "description": "Patient and encouraging. Bob listens for evidence, clarity and reflection.",
        "opening": "Hello, I’m Bob. This is a calm practice interview. I’ll listen closely, then ask you to go deeper where it matters.",
    },
}


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Normalise SQLite's naive timestamps to the UTC-aware app convention."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def bounded_int(value: Any, default: int, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = default if value in (None, "") else int(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{label} must be a whole number.")
    return max(minimum, min(maximum, parsed))


def bounded_float(value: Any, default: float, minimum: float, maximum: float, label: str) -> float:
    try:
        parsed = default if value in (None, "") else float(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{label} must be a number.")
    return max(minimum, min(maximum, parsed))


def parse_bool(value: Any, label: str) -> bool:
    """Parse JSON/native and common form-encoded boolean values strictly."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise HTTPException(422, f"{label} must be true or false.")


def _idempotency_hash(value: str | None) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized[:200].encode("utf-8")).hexdigest()


def _existing_idempotent_session(
    session: Session, user_id: int, key_hash: str
) -> PracticeSession | None:
    if not key_hash:
        return None
    candidates = session.scalars(
        select(PracticeSession)
        .where(PracticeSession.user_id == user_id)
        .order_by(PracticeSession.created_at.desc())
        .limit(100)
    ).all()
    return next(
        (
            row
            for row in candidates
            if (row.config or {}).get("idempotency_key_hash") == key_hash
        ),
        None,
    )


@contextmanager
def db():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def audit(session: Session, action: str, *, actor: User | None = None, outcome: str = "success",
          severity: str = "info", entity_type: str = "", entity_id: str = "", description: str = "",
          request: Request | None = None) -> None:
    # D8: AuditRecord is append-only — never update or delete existing rows.
    ip = _client_ip(request) if request else None
    session.add(AuditRecord(
        actor_id=actor.id if actor else None,
        actor_email=actor.email if actor else "",
        actor_role=actor.role if actor else "",
        action=action[:64], entity_type=entity_type[:64], entity_id=str(entity_id)[:64],
        description=description[:1000] or None, outcome=outcome, severity=severity,
        request_path=request.url.path if request else None,
        request_method=request.method if request else None,
        ip_address=ip,
        user_agent=(request.headers.get("user-agent") if request else "")[:512] or None,
    ))


def public_user(user: User) -> dict[str, Any]:
    points = int(user.total_points or 0)
    level = user.level or ("advanced" if points >= 800 else "intermediate" if points >= 300 else "beginner")
    if level == "advanced":
        level_progress = {"percentage": 100, "points_in_level": max(0, points - 800), "points_to_next": 0, "next_level": None}
    else:
        low, high = (0, 300) if level == "beginner" else (300, 800)
        level_progress = {
            "percentage": min(100, max(0, round((points - low) / max(high - low, 1) * 100))),
            "points_in_level": max(0, points - low),
            "points_to_next": max(0, high - points),
            "next_level": "intermediate" if level == "beginner" else "advanced",
        }
    # D5/PII: phone field is excluded from public projection.
    return {
        "id": user.id, "email": user.email, "full_name": user.full_name, "role": user.role,
        "is_active": user.is_active, "is_verified": user.is_verified,
        "headline": user.headline or "",
        "institution": user.institution or "", "field_of_study": user.field_of_study or "",
        "target_role": user.target_role or "",
        "timezone": user.timezone or "UTC", "avatar_seed": user.avatar_seed or user.email,
        "created_at": user.created_at.strftime("%d %b %Y") if user.created_at else "",
        "last_login_at": user.last_login_at.strftime("%d %b %Y, %H:%M UTC") if user.last_login_at else "",
        "level": level, "total_points": points, "practice_streak": int(user.practice_streak or 0),
        "last_practice_date": user.last_practice_date.isoformat() if user.last_practice_date else None,
        "level_progress": level_progress,
    }


def private_user(user: User) -> dict[str, Any]:
    """Account projection for the signed-in owner, including editable phone data."""
    return public_user(user) | {"phone": user.phone or ""}


def current_user(prepsage_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    if not prepsage_session:
        raise HTTPException(401, "Please sign in to continue.")
    with db() as session:
        record = session.scalar(select(LoginSession).where(
            LoginSession.token_hash == security.hash_token(prepsage_session)
        ))
        expires_at = as_utc(record.expires_at) if record else None
        last_seen_at = as_utc(record.last_seen_at) if record else None
        if not record or record.revoked_at or not expires_at or expires_at <= now():
            raise HTTPException(401, "Your session has expired. Please sign in again.")
        if last_seen_at and now() - last_seen_at > dt.timedelta(hours=12):
            record.revoked_at, record.revoked_reason = now(), "idle_timeout"
            raise HTTPException(401, "You were signed out after a period of inactivity.")
        user = session.get(User, record.user_id)
        if not user or not user.is_active:
            raise HTTPException(401, "This account is no longer available.")
        record.last_seen_at = now()
        # D7: capture last login IP on each authenticated request
        return public_user(user) | {"token": prepsage_session}


def optional_user(prepsage_session: str | None = Cookie(default=None)) -> dict[str, Any] | None:
    """Resolve a session for endpoints that are useful both signed in and out."""
    if not prepsage_session:
        return None
    try:
        return current_user(prepsage_session)
    except HTTPException:
        return None


def require_admin(user: dict[str, Any] = None) -> dict[str, Any]:
    if not user or user["role"] != "admin":
        raise HTTPException(403, "Administrator access is required.")
    return user


def session_view(row: PracticeSession, responses: list[SessionResponse] | None = None) -> dict[str, Any]:
    cfg = row.config or {}
    live_state = cfg.get("live_state") or {}
    provider_info = user_safe_provider_status(
        row.model_name or row.provider,
        pending=bool(cfg.get("provider_pending")),
    )
    live_contribution_count = 0
    if live_state:
        if row.mode == "debate":
            live_contribution_count = len([
                item for item in (live_state.get("speeches") or [])
                if item.get("speaker") == "user" and item.get("type") != "poi"
            ])
        elif row.mode == "group_discussion":
            live_contribution_count = len([
                item for item in (live_state.get("messages") or [])
                if item.get("speaker") == "You"
            ])
    duration = int(row.duration_seconds or 0)
    started_at = as_utc(row.started_at)
    if not duration and started_at:
        end = as_utc(row.completed_at) or now()
        duration = max(0, int((end - started_at).total_seconds()))
    items = []
    for answer in responses or []:
        rubric = (answer.rubric_scores or {}).get("items", [])
        items.append({
            "index": answer.question_index, "number": answer.question_index + 1,
            "question": answer.question_text, "answer": answer.answer_text or "",
            "answer_source": answer.answer_source, "word_count": answer.word_count,
            "score": float(answer.score or 0), "max_score": float(answer.max_score or 10),
            "percent": round(float(answer.score or 0) / max(float(answer.max_score or 10), 1) * 100, 1),
            "feedback": answer.feedback or "", "strengths": answer.strengths or [],
            "improvements": answer.improvements or [], "rubric": rubric,
            "is_skipped": answer.is_skipped, "is_graded": answer.is_graded,
            "response_seconds": answer.response_seconds, "error_message": answer.error_message or "",
        })
    questions = [
        {"index": idx, "number": idx + 1, "prompt": item.get("prompt", ""),
         "focus": item.get("focus", ""), "guidance": item.get("guidance", ""),
         "kind": item.get("kind", "main"), "follow_up_used": bool(item.get("follow_up_used", False)),
         "parent_index": item.get("parent_index")}
        for idx, item in enumerate(row.question_plan or []) if isinstance(item, dict)
    ]
    interviewer_key = str(cfg.get("interviewer", "lisa"))
    interviewer = INTERVIEWERS.get(interviewer_key, INTERVIEWERS["lisa"])
    interview_messages = [
        {"speaker": str(item.get("speaker", "interviewer"))[:24], "content": str(item.get("content", ""))[:1200]}
        for item in (cfg.get("interview_messages") or [])
        if isinstance(item, dict) and str(item.get("content", "")).strip()
    ][-20:]
    strengths = row.strengths or []
    improvements = row.improvements or []
    if row.mode == "interview":
        old_strength_placeholders = {
            "you worked through the interview prompts",
            "you worked through the full interview",
            "you worked through the full rehearsal",
            "your answers were recorded for review",
            "you completed every prompt in the room",
        }
        old_improvement_placeholders = {
            "add a specific example and measurable outcome to your next answer",
            "add one specific example and measurable outcome to your next answer",
            "use the report to identify one focus for your next practice",
            "answer more prompts in your next rehearsal for scored feedback",
        }

        def normalized_point(point: Any) -> str:
            return re.sub(r"[^a-z0-9]+", " ", str(point or "").lower()).strip()

        has_old_placeholders = any(
            normalized_point(point) in old_strength_placeholders for point in strengths
        ) or any(
            normalized_point(point) in old_improvement_placeholders for point in improvements
        )
        if has_old_placeholders or (
            row.status == "completed" and (not strengths or not improvements)
        ):
            tailored_strengths, tailored_improvements = interview_summary_points(responses or [])
            strengths = [
                point for point in strengths
                if normalized_point(point) not in old_strength_placeholders
            ] or tailored_strengths
            improvements = [
                point for point in improvements
                if normalized_point(point) not in old_improvement_placeholders
            ] or tailored_improvements
    return {
        "public_id": row.public_id, "mode": row.mode, "mode_label": MODE_LABELS.get(row.mode, row.mode),
        "title": row.title, "status": row.status, "status_label": STATUS_LABELS.get(row.status, row.status),
        "is_live": bool(live_state), "live_contribution_count": live_contribution_count,
        "difficulty": row.difficulty, "role_target": row.role_target, "topic": row.topic,
        "company": cfg.get("company", ""), "context_notes": cfg.get("context_notes", ""),
        "side": cfg.get("side", ""), "debate_format": cfg.get("debate_format", ""),
        "rounds": cfg.get("rounds", ""), "participants": cfg.get("participants", ""),
        "focus_area": cfg.get("focus_area", ""), "question_count": row.question_count,
        "current_index": row.current_index, "answered_count": row.answered_count,
        "total_score": float(row.total_score or 0), "max_score": float(row.max_score or 0),
        "percentage_score": float(row.percentage_score or 0), "readiness_score": float(row.readiness_score or 0),
        "summary_feedback": row.summary_feedback or "", "strengths": strengths,
        "improvements": improvements, "rubric": (row.rubric_breakdown or {}).get("items", []),
        "transcript": row.transcript or "", "tokens_used": row.tokens_used, "model_name": row.model_name or "",
        "provider": row.provider or "local", "error_message": row.error_message or "",
        "provider_degraded": provider_info["degraded"],
        "provider_notice": provider_info["notice"],
        "provider_pending": bool(cfg.get("provider_pending")),
        "duration_seconds": duration, "duration_label": f"{duration // 60} min {duration % 60}s",
        "created_label": row.created_at.strftime("%d %b %Y, %H:%M UTC"), "questions": questions,
        "responses": items, "document_id": row.document_id or 0,
         "language": row.language or "en", "live_state": cfg.get("live_state"),
        "interview_type": cfg.get("interview_type", ""),
        "project_info": cfg.get("project_info", ""),
         "interviewer": interviewer | {"key": interviewer_key},
         "interview_minutes": int(cfg.get("interview_minutes", 0) or 0),
         "time_limit_seconds": int(row.time_limit_seconds or 0),
         "interview_messages": interview_messages,
         "tab_switch_count": int(cfg.get("tab_switch_count", 0) or 0),
        "wudc": wudc_tier(wudc_score(float(row.percentage_score or 0))) if row.mode == "debate" and row.status == "completed" else None,
        "is_resumable": row.status in {"created", "in_progress", "paused"},
    }


def owned_session(session: Session, user_id: int, public_id: str) -> PracticeSession:
    row = session.scalar(select(PracticeSession).where(
        PracticeSession.user_id == user_id, PracticeSession.public_id == public_id
    ))
    if not row:
        raise HTTPException(404, "We could not find that session on your account.")
    return row


def _progress_view(user: User, achievements: list[Achievement] | None = None) -> dict[str, Any]:
    """Serialize points, level, streak and the previous app's badge system."""
    points = int(user.total_points or 0)
    level = user.level or ("advanced" if points >= 800 else "intermediate" if points >= 300 else "beginner")
    low, high = {"beginner": (0, 300), "intermediate": (300, 800), "advanced": (800, 800)}[level]
    percentage = 100 if level == "advanced" else min(100, max(0, round((points - low) / max(high - low, 1) * 100)))
    earned = achievements or []
    return {
        "level": level,
        "total_points": points,
        "practice_streak": int(user.practice_streak or 0),
        "last_practice_date": user.last_practice_date.isoformat() if user.last_practice_date else None,
        "level_progress": {
            "percentage": percentage,
            "points_in_level": max(0, points - low),
            "points_to_next": 0 if level == "advanced" else max(0, high - points),
            "next_level": None if level == "advanced" else ("intermediate" if level == "beginner" else "advanced"),
        },
        "achievements": [
            {"key": item.achievement_key, "name": item.name, "icon": item.icon,
             "description": item.description,
             "earned_at": item.earned_at.strftime("%d %b %Y") if item.earned_at else ""}
            for item in earned
        ],
        "achievement_definitions": list(ACHIEVEMENT_DEFINITIONS),
    }


def award_completion(
    session: Session,
    user_id: int,
    mode: str,
    score_100: float,
    language: str = "en",
    leadership_score_100: float | None = None,
) -> dict[str, Any]:
    """Persist points and unlock practice milestones exactly once per completion.

    H6: Uses INSERT-ignore pattern via the UniqueConstraint on (user_id,
    achievement_key) so concurrent calls cannot produce duplicate awards.
    """
    user = session.get(User, user_id)
    if not user:
        return {"points_earned": 0, "new_achievements": [], "progress": {}}
    score = max(0.0, min(100.0, float(score_100 or 0)))
    points = max(5, int(round(score / 10)))
    user.total_points = int(user.total_points or 0) + points
    user.level = "advanced" if user.total_points >= 800 else "intermediate" if user.total_points >= 300 else "beginner"
    today = now().date()
    if user.last_practice_date is None:
        user.practice_streak = 1
    elif user.last_practice_date != today:
        user.practice_streak = int(user.practice_streak or 0) + 1 if (today - user.last_practice_date).days == 1 else 1
    user.last_practice_date = today

    completed = session.scalar(select(func.count(PracticeSession.id)).where(
        PracticeSession.user_id == user_id, PracticeSession.status == "completed"
    )) or 0
    # H6: load existing keys to avoid duplicate inserts
    existing = {item.achievement_key for item in session.scalars(
        select(Achievement).where(Achievement.user_id == user_id)
    ).all()}
    leadership = score if leadership_score_100 is None else leadership_score_100
    keys: set[str] = set()
    if mode == "debate":
        keys.add("first_debate")
        if score >= 80: keys.add("debate_master")
        if score >= 90: keys.add("perfect_debate")
    elif mode == "interview":
        keys.add("first_interview")
        if score >= 80: keys.add("interview_pro")
    elif mode == "group_discussion":
        keys.add("first_gd")
        if leadership >= 80: keys.add("gd_leader")
    if completed >= 5: keys.add("sessions_5")
    if completed >= 10: keys.add("sessions_10")
    if score >= 80: keys.add("score_above_8")
    if language and language != "en": keys.add("multilingual")
    if int(user.practice_streak or 0) >= 7: keys.add("streak_7")
    definitions = {item["key"]: item for item in ACHIEVEMENT_DEFINITIONS}
    new_achievements = []
    for key in sorted(keys - existing):
        definition = definitions[key]
        achievement = Achievement(
            user_id=user_id, achievement_key=key, name=definition["name"],
            icon=definition["icon"], description=definition["description"],
        )
        achievement.earned_at = now()
        session.add(achievement)
        new_achievements.append({
            "key": key, "name": definition["name"], "icon": definition["icon"],
            "description": definition["description"],
        })
    session.flush()
    badges = session.scalars(select(Achievement).where(
        Achievement.user_id == user_id
    ).order_by(Achievement.earned_at.desc())).all()
    return {
        "points_earned": points,
        "new_achievements": new_achievements,
        "progress": _progress_view(user, badges),
    }


def local_plan(mode: str, count: int, values: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    subject = values.get("role_target") or values.get("topic") or values.get("focus_area") or "your goal"
    stems = {
        "interview": ["Tell me about yourself and why this role is a fit.", "Describe a challenge you solved.",
                      "How would you prioritise competing responsibilities?", "What would success look like in this role?"],
        "group_discussion": ["Open the discussion with your position.", "Respond to a strong opposing view.",
                             "Bring in a practical example.", "Summarise a constructive way forward."],
        "debate": ["Set out your case and framing.", "Develop your strongest substantive argument.",
                   "Answer the central point of clash.", "Give a persuasive closing statement."],
        "coaching": ["What outcome are you working toward?", "What has made progress difficult so far?",
                    "Which strength can you use more deliberately?", "What is your next concrete action?"],
    }
    if mode == "interview":
        interview_type = values.get("interview_type", "mixed")
        type_stems = {
            "behavioral": ["Tell me about a time you handled a difficult situation.", "Describe a decision you made with incomplete information."],
            "technical": ["Walk me through how you would approach a technical problem in this role.", "Explain a technical concept to a non-specialist stakeholder."],
            "case": ["How would you structure your approach to this business situation?", "What assumptions would you test before recommending a solution?"],
        }
        choices = type_stems.get(interview_type, stems["interview"]) + stems["interview"]
    else:
        choices = stems.get(mode, stems["interview"])
    project = values.get("project_info")
    subject = f"{subject}; draw on {project[:260]}" if project else subject
    questions = [{"prompt": f"{prompt} Keep {subject} in focus.", "focus": RUBRICS[mode][idx % len(RUBRICS[mode])],
                  "guidance": "Use a clear structure, a specific example, and a purposeful conclusion."}
                 for idx, prompt in enumerate((choices * 3)[:count])]
    return f"{MODE_LABELS.get(mode, 'Practice')} · {subject}", questions


def local_evaluation(mode: str, answer: str) -> dict[str, Any]:
    words = len(answer.split())
    score = min(9.0, max(3.0, round(3 + min(words, 180) / 35, 1)))
    rubric = [{"criterion": criterion, "score": min(10, round(score + (idx % 2) * .3, 1)),
               "max_score": 10, "comment": "Clear evidence will make this even stronger."}
              for idx, criterion in enumerate(RUBRICS[mode])]
    return {"score": score, "feedback": "You have a useful foundation. Make the example more specific and finish with the outcome.",
            "strengths": ["You addressed the prompt directly.", "Your answer has a clear point of view."],
            "improvements": ["Add one measurable detail or concrete example.", "End by connecting your answer to the goal."],
            "rubric": rubric}


def interview_summary_points(responses: list[Any]) -> tuple[list[str], list[str]]:
    """Build interview-specific summary points from the recorded answer reviews."""
    generic_points = {
        "you addressed the prompt directly",
        "your answer has a clear point of view",
        "add one measurable detail or concrete example",
        "end by connecting your answer to the goal",
        "you worked through the interview prompts",
        "you worked through the full interview",
        "add a specific example and measurable outcome to your next answer",
    }

    def value(response: Any, key: str, default: Any = None) -> Any:
        return response.get(key, default) if isinstance(response, dict) else getattr(response, key, default)

    def score(response: Any) -> float:
        try:
            return float(value(response, "score", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def question_excerpt(response: Any) -> str:
        question = re.sub(r"\s+", " ", str(value(response, "question_text", "") or "")).strip()
        if len(question) > 100:
            question = question[:100].rsplit(" ", 1)[0].rstrip(" ,;:.") + "…"
        return question

    graded = [
        response for response in responses
        if value(response, "is_graded", True) and not value(response, "is_skipped", False)
        and question_excerpt(response)
    ]
    if not graded:
        return [], []

    def answer_points(key: str, ordered: list[Any]) -> list[str]:
        points: list[str] = []
        seen: set[str] = set()
        for response in ordered:
            raw_points = value(response, key, [])
            if not isinstance(raw_points, (list, tuple)):
                continue
            for raw_point in raw_points:
                point = re.sub(r"\s+", " ", str(raw_point or "")).strip()
                normalized = re.sub(r"[^a-z0-9]+", " ", point.lower()).strip()
                if not point or normalized in generic_points or normalized in seen:
                    continue
                seen.add(normalized)
                points.append(point[:300])
                if len(points) == 3:
                    return points
        return points

    strengths = answer_points("strengths", sorted(graded, key=score, reverse=True))
    improvements = answer_points("improvements", sorted(graded, key=score))

    if not strengths:
        strongest = max(graded, key=score)
        strongest_score = score(strongest)
        if strongest_score >= 7:
            strengths.append(
                f'You scored highest on “{question_excerpt(strongest)}” '
                f'({strongest_score:.1f}/10), making it your strongest response.'
            )

    if not improvements:
        weakest = min(graded, key=score)
        weakest_score = score(weakest)
        if weakest_score < 9.5:
            rubric = value(weakest, "rubric_scores", {})
            rubric_items = rubric.get("items", []) if isinstance(rubric, dict) else []
            scored_items = []
            for item in rubric_items if isinstance(rubric_items, list) else []:
                if not isinstance(item, dict) or not str(item.get("criterion", "")).strip():
                    continue
                try:
                    scored_items.append((float(item.get("score")), str(item["criterion"]).strip()))
                except (TypeError, ValueError):
                    continue
            if scored_items:
                criterion_score, criterion = min(scored_items, key=lambda item: item[0])
                improvements.append(
                    f'On “{question_excerpt(weakest)},” strengthen {criterion} '
                    f'({criterion_score:.1f}/10).'
                )
            else:
                improvements.append(
                    f'Your lowest-scoring answer was to “{question_excerpt(weakest)}” '
                    f'({weakest_score:.1f}/10); use its question-level feedback as your next focus.'
                )
    return strengths, improvements


def provider_kind(model: str) -> str:
    """Map a stored model name to the provider that produced it."""
    if model == "local-coach":
        return "local"
    if provider_status()["groq"] and model == provider_status()["groq_model"]:
        return "groq"
    return "google-genai"


async def create_plan(mode: str, count: int, values: dict[str, Any], document_text: str) -> tuple[str, list[dict[str, str]], int, str]:
    # H4: track which provider generated each plan
    if provider_status()["groq"]:
        try:
            title, questions = await practice_plan(mode, count, values, document_text)
            used_model = provider_status()["groq_model"]
            record_provider_event("practice_plan", "success", provider="groq")
            return title, questions, 0, used_model
        except (FeatureProviderError, ValueError, TypeError, KeyError, AttributeError) as exc:
            record_provider_event("practice_plan", "fallback", provider="groq", code=type(exc).__name__)
    if is_configured():
        try:
            result, tokens = await generate_question_plan(mode=mode, question_count=count, config=values, document_text=document_text)
            questions = [item.model_dump() for item in result.questions if item.prompt.strip()][:count]
            if len(questions) == count:
                record_provider_event("practice_plan", "success", provider="google-genai")
                return result.title.strip() or MODE_LABELS[mode], questions, tokens, model_name()
        except (ProviderError, ValueError, TypeError, KeyError, AttributeError) as exc:
            record_provider_event("practice_plan", "fallback", provider="google-genai", code=type(exc).__name__)
    title, questions = local_plan(mode, count, values)
    record_provider_event("practice_plan", "fallback", provider="local")
    return title, questions, 0, "local-coach"


async def grade_answer(mode: str, question: str, answer: str, values: dict[str, Any]) -> tuple[dict[str, Any], int, str]:
    # H4: return provider alongside result so caller can persist it
    if provider_status()["groq"]:
        try:
            result = await practice_evaluation(mode, question, answer, values)
            record_provider_event("answer_grading", "success", provider="groq")
            return result, 0, provider_status()["groq_model"]
        except (FeatureProviderError, ValueError, TypeError, KeyError, AttributeError) as exc:
            record_provider_event("answer_grading", "fallback", provider="groq", code=type(exc).__name__)
    if is_configured():
        try:
            result, tokens = await evaluate_answer(mode=mode, question=question, answer=answer, config=values)
            record_provider_event("answer_grading", "success", provider="google-genai")
            return result.model_dump(), tokens, model_name()
        except (ProviderError, ValueError, TypeError, KeyError, AttributeError) as exc:
            record_provider_event("answer_grading", "fallback", provider="google-genai", code=type(exc).__name__)
    record_provider_event("answer_grading", "fallback", provider="local")
    return local_evaluation(mode, answer), 0, "local-coach"


@app.get("/api/health")
def health() -> Response:
    # Compatibility endpoint: readiness details are retained for older clients.
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "degraded"
    payload = HealthResponse(status="ok" if db_status == "ok" else "degraded", db=db_status)
    if db_status != "ok":
        return JSONResponse(status_code=503, content=payload.model_dump())
    return JSONResponse(status_code=200, content=payload.model_dump())


@app.get("/api/health/live")
def health_live() -> dict[str, str]:
    """Process liveness does not depend on database availability."""
    return {"status": "ok"}


@app.get("/api/health/ready")
def health_ready() -> HealthResponse:
    """Readiness confirms that the configured database can serve requests."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Readiness check failed: %s", exc)
        raise HTTPException(503, "The application is not ready.")
    return HealthResponse(status="ok", db="ok")


@app.get("/api/health/providers")
def health_providers() -> dict[str, Any]:
    """Expose aggregate, secret-free provider health for monitoring."""
    return {
        "providers": provider_status(),
        "metrics": provider_snapshot(),
    }


@app.get("/api/languages")
def get_languages() -> dict[str, dict[str, str]]:
    """Languages shared by text, browser speech, and Groq transcription."""
    return LANGUAGES


@app.post("/api/coach")
async def coach_message(payload: CoachRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    message = payload.message
    history = [item.model_dump() for item in payload.history]
    try:
        reply, language, provider = await coach_reply(
            message,
            history,
            mode=payload.mode,
            language=payload.language,
            user_goal=payload.user_goal,
        )
    except FeatureProviderError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"success": True, "reply": reply, "language": language, "provider": provider}


@app.post("/api/context-guidance")
async def context_guidance_preview(
    payload: ContextGuidanceRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    context = payload.context_notes
    mode = payload.mode
    values = {
        key: getattr(payload, key)
        for key in ("role_target", "topic", "focus_area", "company", "difficulty")
    }
    try:
        guidance, provider = await context_guidance(
            mode,
            context,
            values,
            language=payload.language,
            variation=payload.variation,
            previous_guidance=payload.previous_guidance,
        )
    except FeatureProviderError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"success": True, "guidance": guidance, "provider": provider}


@app.post("/api/speech/transcribe")
async def transcribe_speech(
    audio: UploadFile = File(...),
    language: str = "en",
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    data = await asyncio.to_thread(audio.file.read, 12 * 1024 * 1024 + 1)
    if not data or len(data) > 12 * 1024 * 1024:
        raise HTTPException(422, "Choose a recording smaller than 12 MB.")
    try:
        return await transcribe_audio(data, audio.filename or "answer.webm", audio.content_type or "audio/webm", language)
    except FeatureProviderError as exc:
        # A browser SpeechRecognition session can still provide a transcript
        # when Whisper is not configured. Return a normal JSON response so the
        # UI can select that fallback without treating a microphone action as
        # a broken request.
        return {
            "success": False,
            "transcript": "",
            "language": language if language in LANGUAGES else "en",
            "service": "browser",
            "browser_fallback": True,
            "error": str(exc),
        }


@app.post("/api/speech/synthesize")
async def synthesize_speech(
    payload: SpeechSynthesisRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    """Return server audio when available, with browser playback as fallback."""
    text = payload.text
    language = payload.language
    language = language if language in LANGUAGES else "en"
    result = await synthesize_text(text, language, payload.voice_profile)
    result["speech_language"] = LANGUAGES[language]["speech"]
    return result


@app.post("/api/auth/register")
def register(payload: RegisterRequest, request: Request) -> dict[str, Any]:
    email = security.normalize_email(payload.email)
    name = " ".join(payload.full_name.split())[:160]
    password = payload.password
    errors: dict[str, str] = {}
    if error := security.validate_email(email): errors["email"] = error
    if error := security.validate_password(password, email, name): errors["password"] = error
    if password != payload.confirm_password: errors["confirm_password"] = "Passwords do not match."
    if errors:
        raise HTTPException(422, {"message": "Please correct the highlighted fields.", "fields": errors})
    with db() as session:
        if session.scalar(select(User.id).where(User.email == email)):
            raise HTTPException(409, {"message": "An account with that email already exists.", "fields": {"email": "Try signing in instead."}})
        # C2: public registration can never grant administrative access.
        # Bootstrap an administrator through the protected operations path.
        role = "student"
        salt = security.generate_salt()
        user = User(email=email, full_name=name, password_hash=security.hash_password(password, salt),
                    password_salt=salt, password_updated_at=now(), role=role, avatar_seed=email,
                    is_verified=False)
        session.add(user); session.flush()
        audit(session, "user.register", actor=user, entity_type="app_user", entity_id=str(user.id),
              description=f"Account created with role {role}.", request=request)
    return {"ok": True, "message": "Welcome aboard — please sign in to begin."}


@app.post("/api/auth/bootstrap-admin")
def bootstrap_admin(request: Request) -> dict[str, Any]:
    """Promote an existing account only when deployment supplies an ops token."""
    if not _BOOTSTRAP_ADMIN_TOKEN:
        raise HTTPException(404, "Administrator bootstrap is not enabled.")
    supplied = request.headers.get("x-prepsage-bootstrap-token", "")
    if not supplied or not secrets.compare_digest(supplied, _BOOTSTRAP_ADMIN_TOKEN):
        raise HTTPException(403, "Administrator bootstrap is not authorised.")
    email = security.normalize_email(request.headers.get("x-prepsage-admin-email", ""))
    if not email:
        raise HTTPException(422, "An administrator email is required.")
    with db() as session:
        row = session.scalar(select(User).where(User.email == email))
        if not row:
            raise HTTPException(404, "Create the account before bootstrapping administrator access.")
        row.role = "admin"
        audit(session, "admin.bootstrap", actor=row, entity_type="app_user",
              entity_id=str(row.id), description="Administrator access provisioned.")
    return {"ok": True, "message": "Administrator access provisioned."}


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    email = security.normalize_email(payload.email)
    password = payload.password
    ip = _client_ip(request)
    with db() as session:
        user = session.scalar(select(User).where(User.email == email))
        # H1/C2: check lockout BEFORE password verification to avoid timing leak
        if user:
            locked_until = as_utc(user.locked_until)
            if locked_until and locked_until > now():
                audit(session, "auth.login", actor=user, outcome="failure", severity="warning",
                      description="Login blocked — account locked.", request=request)
                raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
            if not user.is_active:
                audit(session, "auth.login", actor=user, outcome="failure", severity="warning",
                      description="Login attempt on deactivated account.", request=request)
                raise HTTPException(403, "This account has been deactivated. Contact your administrator.")
        if not user or not security.verify_password(password, user.password_salt, user.password_hash, user.password_iterations):
            if user:
                user.failed_login_count = int(user.failed_login_count or 0) + 1
                if user.failed_login_count >= 8:
                    user.locked_until = now() + dt.timedelta(minutes=15)
                    user.failed_login_count = 0
            audit(session, "auth.login", actor=user, outcome="failure", severity="warning",
                  description="Invalid credentials.", request=request)
            raise HTTPException(401, "That email and password combination doesn't match our records.")
        token = security.generate_session_token()
        session.add(LoginSession(
            user_id=user.id, token_hash=security.hash_token(token),
            token_prefix=security.token_prefix(token),
            expires_at=now() + dt.timedelta(days=14), last_seen_at=now(),
            ip_address=ip,
        ))
        # D7: record last login IP on the user row
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now()
        user.last_login_ip = ip[:45]
        audit(session, "auth.login", actor=user, entity_type="app_user", entity_id=str(user.id),
              description="Login succeeded.", request=request)
        view = public_user(user)
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    secure_cookie = request.url.scheme == "https" or forwarded_proto == "https"
    csrf_tok = _csrf_token()
    response.set_cookie("prepsage_session", token, max_age=1209600, httponly=True,
                        samesite="lax", secure=secure_cookie, path="/")
    # H2: send CSRF token in a JS-readable (non-httponly) cookie
    response.set_cookie(_CSRF_COOKIE, csrf_tok, max_age=1209600, httponly=False,
                        samesite="lax", secure=secure_cookie, path="/")
    return {"ok": True, "user": view}


@app.post("/api/auth/logout")
def logout(response: Response, user: dict[str, Any] | None = Depends(optional_user)) -> dict[str, Any]:
    if user:
        with db() as session:
            record = session.scalar(select(LoginSession).where(LoginSession.token_hash == security.hash_token(user["token"])))
            if record: record.revoked_at, record.revoked_reason = now(), "logout"
    response.delete_cookie("prepsage_session", path="/")
    response.delete_cookie(_CSRF_COOKIE, path="/")
    return {"ok": True}


# D6: email verification flow (token issued at registration, confirmed here)
@app.get("/api/auth/verify-email")
def verify_email(token: str, request: Request) -> dict[str, Any]:
    """Confirm an email address using the one-time token sent after registration.

    The token is a 32-byte hex string stored as a SHA-256 hash in
    User.email_verification_token. Deployments without the verification
    migration receive a clear 501 response; schema/programming errors are not
    hidden behind a catch-all exception.
    """
    if not token or len(token) < 32:
        raise HTTPException(422, "Invalid or missing verification token.")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    verification_column = getattr(User, "email_verification_token", None)
    if verification_column is None:
        raise HTTPException(501, "Email verification is not yet enabled on this instance.")
    with db() as session:
        user_row = session.scalar(
            select(User).where(
                verification_column == token_hash,
                User.is_verified.is_(False),
            )
        )
        if not user_row:
            raise HTTPException(404, "This verification link is invalid or has already been used.")
        user_row.is_verified = True
        setattr(user_row, "email_verification_token", None)
        audit(session, "auth.email_verified", actor=user_row, entity_type="app_user",
              entity_id=str(user_row.id), description="Email address verified.", request=request)
    return {"ok": True, "message": "Your email address has been verified. You can now sign in."}


@app.get("/api/auth/me")
def me(user: dict[str, Any] | None = Depends(optional_user)) -> dict[str, Any]:
    if not user:
        return {"user": None}
    with db() as session:
        row = session.get(User, user["id"])
        badges = session.scalars(select(Achievement).where(
            Achievement.user_id == user["id"]
        ).order_by(Achievement.earned_at.desc())).all()
        view = private_user(row) | _progress_view(row, badges)
    return {"user": view}


@app.get("/api/progress")
def get_progress(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """Return progression and achievement data for dashboards and integrations."""
    with db() as session:
        row = session.get(User, user["id"])
        badges = session.scalars(select(Achievement).where(
            Achievement.user_id == user["id"]
        ).order_by(Achievement.earned_at.desc())).all()
        return _progress_view(row, badges)


@app.put("/api/account")
def update_account(payload: AccountUpdateRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    name = " ".join(payload.full_name.split())[:160]
    with db() as session:
        row = session.get(User, user["id"])
        for key in ("headline", "institution", "field_of_study", "target_role", "timezone"):
            setattr(row, key, getattr(payload, key))
        # Preserve private fields when a partial client update omits them.
        if payload.phone is not None:
            row.phone = payload.phone
        row.full_name = name
        audit(session, "user.profile_updated", actor=row, entity_type="app_user", entity_id=str(row.id), description="Profile updated.")
        view = private_user(row)
    return {"message": "Profile saved.", "user": view}


@app.post("/api/account/password")
def change_password(
    payload: PasswordChangeRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    with db() as session:
        row = session.get(User, user["id"])
        if payload.new_password != payload.confirm_new_password:
            raise HTTPException(422, "New passwords do not match.")
        if not security.verify_password(payload.current_password, row.password_salt, row.password_hash, row.password_iterations):
            raise HTTPException(422, "Your current password is incorrect.")
        if error := security.validate_password(payload.new_password, row.email, row.full_name):
            raise HTTPException(422, error)
        salt = security.generate_salt()
        row.password_salt, row.password_hash, row.password_updated_at = (
            salt,
            security.hash_password(payload.new_password, salt),
            now(),
        )
        session.execute(update(LoginSession).where(
            LoginSession.user_id == row.id,
            LoginSession.token_hash != security.hash_token(user["token"]),
            LoginSession.revoked_at.is_(None),
        ).values(revoked_at=now(), revoked_reason="password_changed"))
        audit(session, "auth.password_changed", actor=row, entity_type="app_user", entity_id=str(row.id), description="Password changed.")
    return {"message": "Password updated; other sessions have been signed out."}


@app.post("/api/account/sessions/revoke")
def revoke_other_sessions(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """Revoke every active session except the browser making this request."""
    with db() as session:
        session.execute(update(LoginSession).where(
            LoginSession.user_id == user["id"],
            LoginSession.token_hash != security.hash_token(user["token"]),
            LoginSession.revoked_at.is_(None),
        ).values(revoked_at=now(), revoked_reason="user_requested"))
    return {"message": "All other sessions have been signed out."}


def home_snapshot(user_id: int) -> dict[str, Any]:
    with db() as session:
        user = session.get(User, user_id)
        goals = session.scalars(
            select(PreparationGoal)
            .where(PreparationGoal.user_id == user_id, PreparationGoal.status != "archived")
            .order_by(PreparationGoal.updated_at.desc())
            .limit(100)
        ).all()
        tasks = session.scalars(
            select(LearnerTask)
            .where(LearnerTask.user_id == user_id, LearnerTask.status.in_(["pending", "in_progress"]))
            .order_by(LearnerTask.due_at)
            .limit(100)
        ).all()
        # Summary numbers are aggregate queries over the full tenant/user set.
        session_filters = [PracticeSession.user_id == user_id]
        summary_row = session.execute(
            select(
                func.count(PracticeSession.id).label("total"),
                func.sum(case((PracticeSession.status == "completed", 1), else_=0)).label("completed"),
                func.sum(case((PracticeSession.status.in_(["created", "in_progress", "paused"]), 1), else_=0)).label("open"),
                func.avg(case((PracticeSession.status == "completed", PracticeSession.percentage_score), else_=None)).label("average"),
                func.avg(case((PracticeSession.status == "completed", PracticeSession.readiness_score), else_=None)).label("readiness"),
                func.coalesce(func.sum(PracticeSession.duration_seconds), 0).label("duration"),
            ).where(*session_filters)
        ).one()
        week_sessions = session.scalar(
            select(func.count(PracticeSession.id)).where(
                *session_filters,
                PracticeSession.created_at >= now() - dt.timedelta(days=7),
            )
        ) or 0
        active_goals = session.scalar(
            select(func.count(PreparationGoal.id)).where(
                PreparationGoal.user_id == user_id, PreparationGoal.status == "active"
            )
        ) or 0
        sessions = session.scalars(
            select(PracticeSession)
            .where(*session_filters)
            .order_by(PracticeSession.created_at.desc())
            .limit(200)
        ).all()
        done = [s for s in sessions if s.status == "completed"]
        completed_by_mode = dict(session.execute(
            select(PracticeSession.mode, func.count(PracticeSession.id))
            .where(*session_filters, PracticeSession.status == "completed")
            .group_by(PracticeSession.mode)
        ).all())
        score_trend = [
            {
                "date": item.created_at.strftime("%d %b") if item.created_at else "",
                "mode": item.mode,
                "mode_label": MODE_LABELS.get(item.mode, item.mode),
                "score": round(float(item.percentage_score or 0), 1),
                "readiness": round(float(item.readiness_score or 0), 1),
                "title": item.title,
            }
            for item in reversed(done[-12:])
        ]
        rubric_scores: dict[str, list[float]] = {}
        for item in done:
            for rubric_item in (item.rubric_breakdown or {}).get("items", []):
                criterion = str(rubric_item.get("criterion", "")).strip()
                if criterion:
                    rubric_scores.setdefault(criterion, []).append(float(rubric_item.get("score", 0) or 0))
        strengths = [
            {"criterion": name, "score": round(sum(values) / len(values), 1)}
            for name, values in rubric_scores.items()
            if values
        ]
        strengths.sort(key=lambda item: item["score"], reverse=True)
        areas_to_improve = list(reversed(strengths))
        recommendations = []
        if not done:
            recommendations.append("Complete your first rehearsal so your readiness profile has a baseline.")
        elif len(done) < 2:
            recommendations.append("Complete one more rehearsal to reveal a useful performance trend.")
        if areas_to_improve:
            recommendations.append(f"Give your next sitting extra attention to {areas_to_improve[0]['criterion'].lower()}.")
        if not user.onboarding_completed:
            recommendations.append("Finish your preparation brief so prompts can reflect your target.")
        distribution = {
            mode: int(completed_by_mode.get(mode, 0) or 0)
            for mode in MODE_LABELS
        }
        goal_session_counts = dict(session.execute(
            select(PracticeSession.goal_id, func.count(PracticeSession.id))
            .where(
                PracticeSession.user_id == user_id,
                PracticeSession.status == "completed",
                PracticeSession.goal_id.is_not(None),
            )
            .group_by(PracticeSession.goal_id)
        ).all())
        answers_recorded = session.scalar(
            select(func.count(SessionResponse.id))
            .join(PracticeSession, PracticeSession.id == SessionResponse.session_id)
            .where(
                PracticeSession.user_id == user_id,
                SessionResponse.is_skipped.is_(False),
            )
        ) or 0
        completed_tasks = session.scalar(
            select(func.count(LearnerTask.id)).where(
                LearnerTask.user_id == user_id, LearnerTask.status == "done"
            )
        ) or 0
        badges = session.scalars(select(Achievement).where(
            Achievement.user_id == user_id
        ).order_by(Achievement.earned_at.desc()).limit(100)).all()
        progress = _progress_view(user, badges)
        weekly_target = max(int(user.weekly_practice_target or 0), 0)
        preferences = user.preferences or {}
        return {"onboarding": {"completed": user.onboarding_completed, "full_name": user.full_name, "target_role": user.target_role,
                  "experience_level": user.experience_level, "weekly_target": user.weekly_practice_target,
                  "weekly_minutes": int(preferences.get("weekly_minutes", 60) or 0),
                  "target_date": str(preferences.get("target_date", "") or ""),
                  "focus_note": str(preferences.get("focus_note", "") or ""),
                  "preferred_modes": user.preferred_modes or [], **preferences},
                "summary": {"total_sessions": int(summary_row.total or 0), "completed_sessions": int(summary_row.completed or 0),
                 "open_sessions": int(summary_row.open or 0),
                 "average_score": round(float(summary_row.average or 0), 1),
                 "readiness": round(float(summary_row.readiness or 0), 1),
                 "practice_minutes": round(float(summary_row.duration or 0) / 60),
                  "answers_recorded": int(answers_recorded), "open_tasks": len(tasks),
                  "completed_tasks": int(completed_tasks),
                  "active_goals": int(active_goals),
                "week_sessions": week_sessions, "weekly_target": weekly_target,
                "weekly_percent": min(100, round(week_sessions / weekly_target * 100)) if weekly_target else 0,
                "level": progress["level"], "total_points": progress["total_points"],
                "practice_streak": progress["practice_streak"],
                "level_progress": progress["level_progress"],
                "achievements": progress["achievements"],
                "achievement_definitions": progress["achievement_definitions"]},
                 "analytics": {
                     "score_trend": score_trend,
                     "distribution": distribution,
                     "strengths": strengths[:3],
                     "areas_to_improve": areas_to_improve[:3],
                     "recommendations": recommendations[:4],
                 },
                 "goals": [{"id": g.id, "title": g.title, "description": g.description or "", "mode": g.mode or "",
                           "status": g.status, "priority": g.priority, "target_sessions": g.target_sessions,
                           "target_score": float(g.target_score or 0), "due_date": g.due_date.isoformat() if g.due_date else "",
                            "completed_sessions": int(goal_session_counts.get(g.id, 0) or 0),
                            "progress_percent": min(100, round(
                                int(goal_session_counts.get(g.id, 0) or 0) / g.target_sessions * 100, 1
                            )) if g.target_sessions else (100 if g.status == "achieved" else 0)} for g in goals],
                "tasks": [{"id": t.id, "title": t.title, "details": t.details or "", "mode": t.mode or "",
                           "status": t.status, "priority": t.priority, "estimated_minutes": t.estimated_minutes,
                           "due_at": t.due_at.isoformat() if t.due_at else ""} for t in tasks],
                "activity": [session_view(s) for s in sessions[:6]]}


@app.get("/api/home")
def get_home(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return home_snapshot(user["id"])


@app.post("/api/onboarding")
def save_onboarding(payload: OnboardingRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as session:
        row = session.get(User, user["id"])
        row.target_role = payload.target_role[:120]
        row.experience_level = payload.experience_level[:32]
        row.weekly_practice_target = payload.weekly_target
        row.preferred_modes = [m for m in payload.preferred_modes if m in MODE_LABELS]
        row.preferences = {
            "weekly_minutes": payload.weekly_minutes,
            "target_date": payload.target_date,
            "focus_note": payload.focus_note[:600],
        }
        row.onboarding_completed = True
        row.onboarding_completed_at = now()
        audit(session, "user.onboarding_saved", actor=row, entity_type="app_user",
              entity_id=str(row.id), description="Preparation brief saved.")
    return {"message": "Your preparation brief is saved."}


@app.post("/api/goals")
@app.put("/api/goals/{goal_id}")
def save_goal(payload: GoalRequest, goal_id: int = 0,
              user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    title = payload.title.strip()
    if len(title) < 1:
        raise HTTPException(422, "Give this preparation goal a clear title.")
    with db() as session:
        row = session.get(PreparationGoal, goal_id) if goal_id else None
        if row and row.user_id != user["id"]:
            raise HTTPException(404, "Goal not found.")
        if not row:
            # D3: associate goal with user explicitly
            row = PreparationGoal(user_id=user["id"], title=title)
            session.add(row)
        row.title = title
        row.description = payload.description[:1000]
        row.mode = payload.mode if payload.mode in MODE_LABELS else None
        if payload.status not in {"active", "achieved", "paused", "archived"}:
            raise HTTPException(422, "Unknown goal state.")
        row.status = payload.status
        row.priority = payload.priority
        row.target_sessions = payload.target_sessions
        row.target_score = payload.target_score or None
        try:
            row.due_date = dt.date.fromisoformat(payload.due_date) if payload.due_date else None
        except ValueError:
            raise HTTPException(422, "Use a valid target date.")
    return {"message": "Goal saved."}


@app.patch("/api/goals/{goal_id}")
def update_goal_status(
    goal_id: int,
    payload: GoalStatusRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """Change only a goal's status without replacing its other fields."""
    with db() as session:
        row = session.get(PreparationGoal, goal_id)
        if not row or row.user_id != user["id"]:
            raise HTTPException(404, "Goal not found.")
        row.status = payload.status
    return {"message": "Goal updated."}


@app.post("/api/tasks")
def save_task(payload: TaskRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    title = payload.title.strip()
    if len(title) < 1:
        raise HTTPException(422, "Give this task a clear title.")
    due_at = None
    if payload.due_date:
        try:
            due_at = dt.datetime.combine(
                dt.date.fromisoformat(payload.due_date), dt.time.min, tzinfo=dt.timezone.utc
            )
        except ValueError:
            raise HTTPException(422, "Use a valid task date.")
    with db() as session:
        goal = None
        if payload.goal_id:
            goal = session.get(PreparationGoal, payload.goal_id)
            if not goal or goal.user_id != user["id"]:
                raise HTTPException(404, "Goal not found.")
        session.add(LearnerTask(
            user_id=user["id"], title=title,
            goal_id=goal.id if goal else None,
            details=payload.details[:1000],
            mode=payload.mode if payload.mode in MODE_LABELS else None,
            priority=payload.priority,
            estimated_minutes=payload.estimated_minutes,
            due_at=due_at,
        ))
    return {"message": "Task added."}


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, payload: TaskStatusRequest,
                user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if payload.status not in {"pending", "in_progress", "done", "skipped"}:
        raise HTTPException(422, "Status must be pending, in_progress, done, or skipped.")
    with db() as session:
        row = session.get(LearnerTask, task_id)
        if not row or row.user_id != user["id"]:
            raise HTTPException(404, "Task not found.")
        row.status = payload.status
        row.completed_at = now() if payload.status == "done" else None
    return {"message": "Task updated."}


@app.get("/api/documents")
def get_documents(
    limit: int = 50,
    offset: int = 0,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    with db() as session:
        filters = [
            UploadedDocument.user_id == user["id"],
            UploadedDocument.is_deleted.is_(False),
        ]
        total = session.scalar(
            select(func.count(UploadedDocument.id)).where(*filters)
        ) or 0
        rows = session.scalars(
            select(UploadedDocument)
            .where(*filters)
            .order_by(UploadedDocument.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return {
            "documents": [
                {
                    "id": r.id,
                    "name": r.original_filename,
                    "extension": r.extension,
                    "size_kb": max(1, r.size_bytes // 1024),
                    "characters": len(r.extracted_text or ""),
                    "scan_status": r.scan_status,
                }
                for r in rows
            ],
            "total": int(total),
            "limit": limit,
            "offset": offset,
        }


@app.post("/api/documents")
def upload_document(file: UploadFile = File(...), user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    # H7: validate extension
    name = Path(file.filename or "").name
    extension = Path(name).suffix.lower()
    if extension not in {".pdf", ".docx", ".txt"}:
        raise HTTPException(422, "Only PDF, DOCX or TXT files up to 2 MB can be used as context.")
    data = file.file.read(2 * 1024 * 1024 + 1)
    if not data or len(data) > 2 * 1024 * 1024:
        raise HTTPException(422, "Only PDF, DOCX or TXT files up to 2 MB can be used as context.")
    declared_type = (file.content_type or "").lower().split(";", 1)[0].strip()
    allowed_types = {
        ".pdf": {"application/pdf"},
        ".docx": {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/zip",
            "application/octet-stream",
        },
        ".txt": {"text/plain", "application/octet-stream", ""},
    }
    if declared_type not in allowed_types[extension]:
        raise HTTPException(422, "The uploaded file type does not match its extension.")
    # H7: magic-byte validation
    _validate_upload_magic(data, extension)
    # H9: only allow extraction when scan_status will be set to "extracted"
    if extension == ".txt":
        text = data.decode("utf-8", errors="replace")
    elif extension == ".docx":
        # H8: archive bomb guard inside _safe_docx_text
        try:
            text = _safe_docx_text(data)
        except DocumentExtractionError as exc:
            _record_document_scan_failure(
                data=data,
                name=name,
                extension=extension,
                content_type=file.content_type or "",
                user_id=user["id"],
            )
            logger.warning(
                "document_extraction_failed code=%s extension=%s user_id=%s",
                exc.code,
                extension,
                user["id"],
            )
            raise HTTPException(exc.status_code, exc.message) from exc
    else:
        try:
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError
        except ImportError as exc:
            _record_document_scan_failure(
                data=data,
                name=name,
                extension=extension,
                content_type=file.content_type or "",
                user_id=user["id"],
            )
            logger.exception(
                "document_extraction_dependency_unavailable extension=%s user_id=%s",
                extension,
                user["id"],
            )
            raise HTTPException(
                503,
                "Document processing is temporarily unavailable. Please try again later.",
            ) from exc
        try:
            reader = PdfReader(BytesIO(data))
            if len(reader.pages) > 100:
                raise HTTPException(422, "PDFs are limited to 100 pages.")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except HTTPException:
            raise
        except PdfReadError as exc:
            _record_document_scan_failure(
                data=data,
                name=name,
                extension=extension,
                content_type=file.content_type or "",
                user_id=user["id"],
            )
            logger.warning(
                "document_extraction_failed code=pdf_parse_failed extension=%s user_id=%s",
                extension,
                user["id"],
            )
            raise HTTPException(
                422,
                "We could not process this PDF. It may be corrupt or unsupported.",
            ) from exc
        except (KeyError, OSError, TypeError, ValueError) as exc:
            _record_document_scan_failure(
                data=data,
                name=name,
                extension=extension,
                content_type=file.content_type or "",
                user_id=user["id"],
            )
            logger.warning(
                "document_extraction_failed code=pdf_read_failed extension=%s user_id=%s",
                extension,
                user["id"],
            )
            raise HTTPException(
                422,
                "We could not process this PDF. It may be corrupt or unsupported.",
            ) from exc
    text = re.sub(r"\s+", " ", text).strip()[:20000]
    if len(text) < 40:
        _record_document_scan_failure(
            data=data,
            name=name,
            extension=extension,
            content_type=file.content_type or "",
            user_id=user["id"],
            status="insufficient_text",
        )
        logger.info(
            "document_extraction_rejected code=insufficient_text extension=%s user_id=%s",
            extension,
            user["id"],
        )
        raise HTTPException(422, "We could not read enough text from that file. Please upload a text-based document.")
    stored = f"{secrets.token_hex(16)}{extension}"
    stored_path = UPLOAD_DIR / stored
    try:
        try:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(503, "Document storage is unavailable.") from exc
        stored_path.write_bytes(data)
        with db() as session:
            row = UploadedDocument(
                user_id=user["id"],
                original_filename=re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:255],
                stored_filename=stored,
                document_type=extension.lstrip("."),
                content_type=(file.content_type or "")[:128],
                extension=extension,
                size_bytes=len(data),
                checksum_sha256=hashlib.sha256(data).hexdigest(),
                extracted_text=text,
                # H9: only mark "extracted" after successful text extraction
                scan_status="extracted",
            )
            session.add(row)
            session.flush()
            result = {
                "message": f"{row.original_filename} is ready as context.",
                "document": {"id": row.id, "name": row.original_filename},
            }
    except (OSError, SQLAlchemyError, HTTPException):
        try:
            stored_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return result


@app.get("/api/practice")
def list_practice(
    search: str = "",
    mode: str = "all",
    status: str = "all",
    difficulty: str = "all",
    min_score: str = "",
    limit: int = 50,
    offset: int = 0,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    # D4: paginated query — no unbounded SELECT * on large tables
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    with db() as session:
        filters = [PracticeSession.user_id == user["id"]]
        if mode in MODE_LABELS:
            filters.append(PracticeSession.mode == mode)
        if status in STATUS_LABELS:
            filters.append(PracticeSession.status == status)
        if difficulty in {"beginner", "intermediate", "advanced", "expert"}:
            filters.append(PracticeSession.difficulty == difficulty)
        try:
            minimum_score = float(min_score) if str(min_score).strip() else None
        except (TypeError, ValueError):
            minimum_score = None
        if minimum_score is not None:
            filters.append(
                PracticeSession.percentage_score >= max(0, min(100, minimum_score))
            )
        if search.strip():
            term = f"%{search.lower()}%"
            filters.append(or_(
                func.lower(PracticeSession.title).like(term),
                func.lower(PracticeSession.topic).like(term),
                func.lower(PracticeSession.role_target).like(term),
            ))
        statement = select(PracticeSession).where(*filters)
        # Keep the same filter expression for both the page and full-dataset
        # aggregates; no summary value is derived from the current page.
        total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        aggregate = session.execute(
            select(
                func.sum(case((PracticeSession.status == "completed", 1), else_=0)).label("completed"),
                func.sum(case((PracticeSession.status.in_(["created", "in_progress", "paused"]), 1), else_=0)).label("open"),
                func.avg(case((PracticeSession.status == "completed", PracticeSession.percentage_score), else_=None)).label("average"),
                func.coalesce(func.sum(PracticeSession.duration_seconds), 0).label("duration"),
            ).select_from(PracticeSession).where(*filters)
        ).one()
        rows = session.scalars(
            statement.order_by(PracticeSession.created_at.desc())
            .limit(limit).offset(offset)
        ).all()
        return {
            "sessions": [session_view(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "counts": {
                "total": total,
                "completed": int(aggregate.completed or 0),
                "open": int(aggregate.open or 0),
                "average": round(float(aggregate.average or 0), 1),
                "minutes": round(float(aggregate.duration or 0) / 60),
            },
        }


@app.post("/api/practice")
def start_practice(
    payload: PracticeStartRequest,
    user: dict[str, Any] = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    mode = payload.mode
    if mode not in MODE_LABELS: raise HTTPException(422, "Choose a valid practice mode.")
    count = bounded_int(
        payload.question_count,
        5,
        1 if mode == "interview" else 3,
        5 if mode == "interview" else 10,
        "Prompt count",
    )
    if mode == "interview" and len(payload.role_target.strip()) < 2: raise HTTPException(422, "Name the role you are preparing for.")
    if mode in {"group_discussion", "debate"} and len((payload.topic or payload.motion or "").strip()) < 4: raise HTTPException(422, "Give the motion or discussion topic.")
    debate_side = payload.side if mode == "debate" and payload.side else "proposition" if mode == "debate" else ""
    debate_format = (
        payload.debate_format
        if mode == "debate" and payload.debate_format
        else "british_parliamentary"
        if mode == "debate"
        else ""
    )
    values = {
        "role_target": payload.role_target, "company": payload.company,
        "topic": payload.topic or payload.motion or "",
        "context_notes": payload.context_notes,
        "project_info": payload.project_info,
        "difficulty": payload.difficulty, "language": payload.language,
        "interview_type": payload.interview_type,
        "interview_minutes": payload.interview_minutes,
        "interviewer": payload.interviewer_id,
        "side": debate_side,
        "debate_format": debate_format,
        "participants": payload.participants,
        "focus_area": payload.focus_area,
        "rounds": str(payload.rounds),
    }
    doc_text = ""
    key_hash = _idempotency_hash(idempotency_key)
    with db() as session:
        existing = _existing_idempotent_session(session, user["id"], key_hash)
        if existing:
            return {
                "message": "Your rehearsal room is ready.",
                "public_id": existing.public_id,
                "idempotent": True,
                **user_safe_provider_status(existing.model_name or existing.provider),
            }
        if payload.document_id:
            document = session.scalar(select(UploadedDocument).where(
                UploadedDocument.id == payload.document_id,
                UploadedDocument.user_id == user["id"],
                UploadedDocument.is_deleted.is_(False),
            ))
            if not document:
                raise HTTPException(404, "That document is no longer available.")
            # H9: only surface text from documents that completed extraction
            if document.scan_status != "extracted":
                raise HTTPException(422, "That document has not been processed yet.")
            doc_text = document.extracted_text or ""
    title, questions, tokens, provider = _await_on_application_loop(
        create_plan(mode, count, values, doc_text)
    )
    if not questions: raise HTTPException(502, "No prompts were generated. Please try again.")
    time_limit_seconds = 0
    interview_config: dict[str, Any] = {}
    if mode == "interview":
        interviewer_key = values.get("interviewer", "lisa")
        if interviewer_key not in INTERVIEWERS:
            interviewer_key = "lisa"
        interview_minutes = bounded_int(values.get("interview_minutes"), 5, 1, 10, "Interview duration")
        questions = [
            {**item, "kind": "main", "follow_up_used": False, "parent_index": None}
            for item in questions
        ]
        interviewer = INTERVIEWERS[interviewer_key]
        interview_config = {
            "interviewer": interviewer_key,
            "interview_minutes": interview_minutes,
            "interview_messages": [{"speaker": "interviewer", "content": interviewer["opening"]}],
            "tab_switch_count": 0,
        }
        time_limit_seconds = interview_minutes * 60
    with db() as session:
        row = PracticeSession(
            public_id=str(uuid4()), user_id=user["id"],
            document_id=payload.document_id or None,
            mode=mode, title=title[:200], status="in_progress",
            role_target=values["role_target"], topic=values["topic"],
            difficulty=values["difficulty"] if values["difficulty"] in {"beginner", "intermediate", "advanced", "expert"} else "intermediate",
            language=values["language"] if values["language"] in LANGUAGES else "en",
            question_count=len(questions), question_plan=questions,
            max_score=float(len(questions) * 10),
            time_limit_seconds=time_limit_seconds,
            config={
                **{k: v for k, v in values.items() if k not in {"role_target", "topic", "difficulty"}},
                **interview_config,
                **({"idempotency_key_hash": key_hash} if key_hash else {}),
            },
            started_at=now(), last_activity_at=now(),
            provider=provider_kind(provider), model_name=provider, tokens_used=tokens,
        )
        session.add(row); session.flush(); public_id = row.public_id
    return {
        "message": "Your rehearsal room is ready.",
        "public_id": public_id,
        **user_safe_provider_status(provider),
    }


@app.get("/api/practice/{public_id}")
def get_practice(public_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        responses = session.scalars(select(SessionResponse).where(SessionResponse.session_id == row.id).order_by(SessionResponse.question_index)).all()
        return session_view(row, responses)


@app.post("/api/practice/{public_id}/answer")
def answer_practice(
    public_id: str,
    payload: AnswerRequest,
    user: dict[str, Any] = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    gamification = {"points_earned": 0, "new_achievements": [], "progress": {}}
    response_seconds = payload.response_seconds
    key_hash = _idempotency_hash(idempotency_key)
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        config = row.config or {}
        idempotency_records = config.get("answer_idempotency_keys") or {}
        if not isinstance(idempotency_records, dict):
            idempotency_records = {}
        if key_hash and (
            key_hash in idempotency_records
            or config.get("last_answer_idempotency_key_hash") == key_hash
        ):
            responses = session.scalars(
                select(SessionResponse)
                .where(SessionResponse.session_id == row.id)
                .order_by(SessionResponse.question_index)
            ).all()
            return session_view(row, responses)
        index, answer = row.current_index, payload.answer.strip()
        if row.status != "in_progress" or index >= len(row.question_plan or []): raise HTTPException(409, "This rehearsal is not awaiting an answer.")
        skipped = payload.skip or not answer
        if not skipped and len(answer) < 20: raise HTTPException(422, "Write at least 20 characters so your coach has something to assess.")
        question = row.question_plan[index]; values = {"role_target": row.role_target, "topic": row.topic, "difficulty": row.difficulty, **(row.config or {})}
        expected_state_version = row.state_version
        all_answers = session.scalars(
            select(SessionResponse).where(SessionResponse.session_id == row.id)
        ).all()
    evaluation = local_evaluation(row.mode, "") if skipped else None
    tokens = 0
    eval_provider = "local-coach"
    if not skipped:
        evaluation, tokens, eval_provider = _await_on_application_loop(
            grade_answer(row.mode, str(question.get("prompt", "")), answer, values)
        )
    completion_summary: dict[str, Any] = {}
    if index + 1 >= len(row.question_plan or []) and provider_status()["groq"]:
        completed_transcript = "\n\n".join(
            f"Q{item.question_index + 1}: {item.question_text}\nA: {item.answer_text or ''}"
            for item in all_answers
        )
        completed_transcript = (
            f"{completed_transcript}\n\nQ{index + 1}: {question.get('prompt', '')}\nA: {answer or ''}"
        ).strip()
        graded_scores = [float(item.score or 0) for item in all_answers if item.is_graded and not item.is_skipped]
        if not skipped and evaluation:
            graded_scores.append(float(evaluation.get("score", 0)))
        average_score = sum(graded_scores) / max(len(graded_scores), 1)
        try:
            completion_summary = _await_on_application_loop(
                practice_summary(row.mode, values, completed_transcript, average_score)
            )
        except (FeatureProviderError, ValueError, TypeError, KeyError):
            completion_summary = {}
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        config = row.config or {}
        idempotency_records = config.get("answer_idempotency_keys") or {}
        if not isinstance(idempotency_records, dict):
            idempotency_records = {}
        if key_hash and (
            key_hash in idempotency_records
            or config.get("last_answer_idempotency_key_hash") == key_hash
        ):
            responses = session.scalars(
                select(SessionResponse)
                .where(SessionResponse.session_id == row.id)
                .order_by(SessionResponse.question_index)
            ).all()
            return session_view(row, responses)
        # Claim this exact session state before inserting the response. Two
        # requests that evaluated the same prompt can therefore not both win.
        claim = session.execute(
            update(PracticeSession)
            .where(
                PracticeSession.id == row.id,
                PracticeSession.status == "in_progress",
                PracticeSession.current_index == index,
                PracticeSession.state_version == expected_state_version,
            )
            .values(state_version=PracticeSession.state_version + 1)
        )
        if claim.rowcount != 1:
            existing = session.scalar(
                select(SessionResponse).where(
                    SessionResponse.session_id == row.id,
                    SessionResponse.question_index == index,
                )
            )
            if existing:
                raise HTTPException(409, "This answer was already recorded. Refresh the page to continue.")
            raise HTTPException(409, "This rehearsal has moved on. Refresh the page and try again.")
        row.state_version = expected_state_version + 1
        response = SessionResponse(session_id=row.id, question_index=index, question_text=str(question.get("prompt", "")),
                                   answer_text=answer or None, answer_source="skipped" if skipped else payload.source[:16],
                                   word_count=len(answer.split()), score=0 if skipped else float(evaluation["score"]), max_score=10,
                                   rubric_scores={"items": [] if skipped else evaluation["rubric"]},
                                   feedback=None if skipped else evaluation["feedback"],
                                   strengths=[] if skipped else evaluation["strengths"],
                                   improvements=[] if skipped else evaluation["improvements"], is_skipped=skipped,
                                   is_graded=not skipped,
                                    answered_at=now(), response_seconds=response_seconds)
        try:
            with session.begin_nested():
                session.add(response)
                session.flush()
        except IntegrityError as exc:
            logger.warning(
                "practice_answer_claim_conflict session_id=%s question_index=%s",
                row.id,
                index,
            )
            raise HTTPException(409, "This answer was already recorded. Refresh the page to continue.") from exc
        all_answers = session.scalars(select(SessionResponse).where(SessionResponse.session_id == row.id)).all()
        graded = [item for item in all_answers if item.is_graded and not item.is_skipped]
        row.answered_count, row.total_score = len(graded), sum(float(item.score or 0) for item in graded)
        row.current_index, row.percentage_score = index + 1, round(float(row.total_score) / max(len(row.question_plan or []) * 10, 1) * 100, 2)
        row.tokens_used += tokens
        # H4: persist which provider evaluated this answer
        if not skipped and eval_provider:
            row.provider = provider_kind(eval_provider)
            row.model_name = eval_provider
        if key_hash:
            idempotency_records[key_hash] = {"question_index": index}
            # Keep durable history without allowing request headers to grow
            # the session JSON without bound.
            idempotency_records = dict(list(idempotency_records.items())[-64:])
            row.config = {
                **(row.config or {}),
                "answer_idempotency_keys": idempotency_records,
                "last_answer_idempotency_key_hash": key_hash,
            }
        row.last_activity_at = now()
        started_at = as_utc(row.started_at)
        if started_at:
            row.duration_seconds = max(0, int((now() - started_at).total_seconds()))
        if row.current_index >= len(row.question_plan or []): row.status = "completed"; row.completed_at = now()
        if row.status == "completed":
            transcript = "\n\n".join(f"Q{item.question_index + 1}: {item.question_text}\nA: {item.answer_text}" for item in all_answers)
            row.transcript = transcript
            average = float(row.total_score) / max(len(graded), 1)
            row.readiness_score = round(average * 10, 1)
            interview_strengths, interview_improvements = (
                interview_summary_points(all_answers) if row.mode == "interview" else ([], [])
            )
            if completion_summary:
                row.readiness_score = float(completion_summary.get("readiness", row.readiness_score))
                row.summary_feedback = str(completion_summary.get("summary", ""))[:2000]
                row.strengths = completion_summary.get("strengths") or interview_strengths
                row.improvements = completion_summary.get("improvements") or interview_improvements
                row.rubric_breakdown = {"items": completion_summary.get("rubric") or []}
            else:
                row.summary_feedback = "You completed the rehearsal. Review each note, then repeat the areas that need clearer evidence." if graded else "You completed the rehearsal. Try answering the skipped prompts next time so your coach can assess your readiness."
                if row.mode == "interview":
                    row.strengths, row.improvements = interview_strengths, interview_improvements
                else:
                    row.strengths = ["You worked through the full rehearsal.", "Your answers were recorded for review."] if graded else ["You completed every prompt in the room."]
                    row.improvements = ["Use the report to identify one focus for your next practice."] if graded else ["Answer more prompts in your next rehearsal for scored feedback."]
                row.rubric_breakdown = {"items": [{"criterion": r, "score": round(average, 1), "max_score": 10, "comment": "Continue building this area."} for r in RUBRICS[row.mode]]}
            gamification = award_completion(
                session, user["id"], row.mode, float(row.percentage_score or 0),
                row.language,
            )
        responses = session.scalars(select(SessionResponse).where(SessionResponse.session_id == row.id).order_by(SessionResponse.question_index)).all()
        return session_view(row, responses) | {"gamification": gamification}


_INTERVIEW_TURN_CLAIM_LEASE = dt.timedelta(minutes=5)


def _interview_turn_claim_is_active(config: dict[str, Any]) -> bool:
    claim = config.get("interview_turn_claim")
    if not isinstance(claim, dict):
        return False
    try:
        claimed_at = dt.datetime.fromisoformat(str(claim.get("started_at", "")))
    except (TypeError, ValueError):
        return False
    claimed_at = as_utc(claimed_at)
    return bool(claimed_at and now() - claimed_at < _INTERVIEW_TURN_CLAIM_LEASE)


def _release_interview_turn_claim(
    user_id: int, public_id: str, claim_token: str
) -> None:
    try:
        with db() as session:
            row = session.scalar(
                select(PracticeSession).where(
                    PracticeSession.user_id == user_id,
                    PracticeSession.public_id == public_id,
                )
            )
            if not row:
                return
            config = dict(row.config or {})
            claim = config.get("interview_turn_claim")
            if not isinstance(claim, dict) or claim.get("token") != claim_token:
                return
            config.pop("interview_turn_claim", None)
            version = int(row.state_version or 0)
            session.execute(
                update(PracticeSession)
                .where(
                    PracticeSession.id == row.id,
                    PracticeSession.user_id == user_id,
                    PracticeSession.state_version == version,
                )
                .values(
                    config=config,
                    state_version=PracticeSession.state_version + 1,
                )
            )
    except Exception:
        logger.exception("Could not release an interview-turn claim.")


@app.post("/api/interview/{public_id}/turn")
def interview_room_turn(
    public_id: str,
    payload: InterviewTurnRequest,
    user: dict[str, Any] = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Claim, evaluate and atomically record one candidate turn."""
    key_hash = _idempotency_hash(idempotency_key)
    claim_token = secrets.token_urlsafe(32)
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        config = dict(row.config or {})
        turn_history = config.get("turn_idempotency_keys")
        if not isinstance(turn_history, dict):
            turn_history = {}
        if key_hash and (
            key_hash in turn_history
            or config.get("last_turn_idempotency_key_hash") == key_hash
        ):
            responses = session.scalars(
                select(SessionResponse)
                .where(SessionResponse.session_id == row.id)
                .order_by(SessionResponse.question_index)
            ).all()
            return {
                "success": True,
                "session": session_view(row, responses),
                "interviewer_reply": "",
                "follow_up": False,
                "completed": row.status == "completed",
                "provider": row.model_name or row.provider or "local",
                "idempotent": True,
                "gamification": {"points_earned": 0, "new_achievements": [], "progress": {}},
            }
        if row.mode != "interview":
            raise HTTPException(409, "This room is not an interview.")
        if row.status != "in_progress":
            raise HTTPException(409, "This interview is not awaiting a response.")
        if _interview_turn_claim_is_active(config):
            raise HTTPException(409, "An interview response is already being processed.")
        plan = [dict(item) for item in (row.question_plan or []) if isinstance(item, dict)]
        index = int(row.current_index or 0)
        if index >= len(plan):
            raise HTTPException(409, "This interview has already ended.")
        started_at = as_utc(row.started_at)
        elapsed = max(0, int((now() - started_at).total_seconds())) if started_at else 0
        if row.time_limit_seconds and elapsed >= row.time_limit_seconds:
            raise HTTPException(
                409,
                "Interview time has ended. Open the report to review your recorded answers.",
            )

        expected_version = int(row.state_version or 0)
        session_id = row.id
        role_target = row.role_target
        language = row.language or "en"
        question = plan[index]
        answer = payload.answer
        current_messages = [
            dict(item)
            for item in (config.get("interview_messages") or [])
            if isinstance(item, dict)
        ]
        prior_responses = session.scalars(
            select(SessionResponse)
            .where(SessionResponse.session_id == session_id)
            .order_by(SessionResponse.question_index)
        ).all()
        values = {
            "role_target": role_target,
            "company": config.get("company", ""),
            "interview_type": config.get("interview_type", "mixed"),
            "project_info": config.get("project_info", ""),
            "context_notes": config.get("context_notes", ""),
            "difficulty": row.difficulty,
        }
        follow_up_used = bool(question.get("follow_up_used", False))
        remaining_questions = [
            str(item.get("prompt", "")).strip()
            for item in plan[index + 1 :]
            if item.get("kind", "main") == "main" and str(item.get("prompt", "")).strip()
        ]
        config.pop("interview_turn_claim", None)
        config["interview_turn_claim"] = {
            "token": claim_token,
            "started_at": now().isoformat(),
            "key_hash": key_hash,
        }
        claimed = session.execute(
            update(PracticeSession)
            .where(
                PracticeSession.id == session_id,
                PracticeSession.user_id == user["id"],
                PracticeSession.mode == "interview",
                PracticeSession.status == "in_progress",
                PracticeSession.current_index == index,
                PracticeSession.state_version == expected_version,
            )
            .values(
                config=config,
                state_version=PracticeSession.state_version + 1,
            )
        )
        if claimed.rowcount != 1:
            raise HTTPException(409, "This interview is already being updated. Refresh and try again.")

    try:
        evaluation, tokens, eval_provider = _await_on_application_loop(
            grade_answer("interview", str(question.get("prompt", "")), answer, values)
        )
        turn = _await_on_application_loop(
            interview_turn(
                interviewer=str(config.get("interviewer", "lisa")),
                role_target=role_target,
                company=str(config.get("company", "")),
                objective=str(config.get("context_notes") or role_target),
                question=str(question.get("prompt", "")),
                answer=answer,
                transcript=current_messages,
                follow_up_used=follow_up_used,
                remaining_questions=remaining_questions,
                language=language,
            )
        )
        should_follow_up = (
            bool(turn.get("follow_up"))
            and not follow_up_used
            and bool(turn.get("question"))
        )
        if should_follow_up:
            follow_up_question = {
                "prompt": str(turn.get("question", ""))[:1000],
                "focus": "Follow-up",
                "guidance": "Go one layer deeper: name what you did, why you chose it, and what changed.",
                "kind": "follow_up",
                "follow_up_used": True,
                "parent_index": index,
            }
            plan = plan[: index + 1] + [follow_up_question] + plan[index + 1 :]
        messages = current_messages + [
            {"speaker": "user", "content": answer},
            {"speaker": "interviewer", "content": str(turn.get("reply", "")).strip()[:1200]},
        ]
        completed = not should_follow_up and not remaining_questions
        completion_summary: dict[str, Any] = {}
        if completed:
            transcript = "\n\n".join(
                [
                    *[
                        f"Q{item.question_index + 1}: {item.question_text}\nA: {item.answer_text or ''}"
                        for item in prior_responses
                    ],
                    f"Q{index + 1}: {question.get('prompt', '')}\nA: {answer}",
                ]
            )
            graded_scores = [
                float(item.score or 0)
                for item in prior_responses
                if item.is_graded and not item.is_skipped
            ]
            graded_scores.append(float(evaluation.get("score", 0)))
            average_score = sum(graded_scores) / max(len(graded_scores), 1)
            try:
                completion_summary = _await_on_application_loop(
                    practice_summary("interview", values, transcript, average_score)
                )
            except (FeatureProviderError, ValueError, TypeError, KeyError):
                completion_summary = {}

        gamification = {"points_earned": 0, "new_achievements": [], "progress": {}}
        with db() as session:
            row = owned_session(session, user["id"], public_id)
            active_config = dict(row.config or {})
            active_claim = active_config.get("interview_turn_claim")
            if (
                row.status != "in_progress"
                or int(row.current_index or 0) != index
                or not isinstance(active_claim, dict)
                or active_claim.get("token") != claim_token
            ):
                raise HTTPException(409, "This interview has moved on. Refresh the room and try again.")
            claimed_version = expected_version + 1
            committed = session.execute(
                update(PracticeSession)
                .where(
                    PracticeSession.id == session_id,
                    PracticeSession.user_id == user["id"],
                    PracticeSession.status == "in_progress",
                    PracticeSession.current_index == index,
                    PracticeSession.state_version == claimed_version,
                )
                .values(state_version=PracticeSession.state_version + 1)
            )
            if committed.rowcount != 1:
                raise HTTPException(
                    409,
                    "This interview changed while your response was being prepared. Refresh and try again.",
                )
            row = session.scalar(
                select(PracticeSession)
                .where(PracticeSession.id == session_id)
                .execution_options(populate_existing=True)
            )
            if not row:
                raise HTTPException(409, "This interview is no longer available.")
            config = dict(row.config or {})
            config.pop("interview_turn_claim", None)
            config["interview_messages"] = messages[-20:]
            config["tab_switch_count"] = payload.tab_switch_count
            if key_hash:
                turn_history = config.get("turn_idempotency_keys")
                if not isinstance(turn_history, dict):
                    turn_history = {}
                turn_history[key_hash] = {"question_index": index}
                config["turn_idempotency_keys"] = dict(list(turn_history.items())[-64:])
                config["last_turn_idempotency_key_hash"] = key_hash

            response = SessionResponse(
                session_id=row.id,
                question_index=index,
                question_text=str(question.get("prompt", "")),
                answer_text=answer,
                answer_source=payload.source,
                word_count=len(answer.split()),
                score=float(evaluation["score"]),
                max_score=10,
                rubric_scores={"items": evaluation["rubric"]},
                feedback=evaluation["feedback"],
                strengths=evaluation["strengths"],
                improvements=evaluation["improvements"],
                is_skipped=False,
                is_graded=True,
                answered_at=now(),
                response_seconds=payload.response_seconds,
            )
            try:
                with session.begin_nested():
                    session.add(response)
                    session.flush()
            except IntegrityError as exc:
                raise HTTPException(
                    409,
                    "This interview answer was already recorded. Refresh the room to continue.",
                ) from exc

            all_answers = session.scalars(
                select(SessionResponse)
                .where(SessionResponse.session_id == row.id)
                .order_by(SessionResponse.question_index)
            ).all()
            graded = [item for item in all_answers if item.is_graded and not item.is_skipped]
            row.config = config
            row.question_plan = plan
            row.question_count = len(plan)
            row.current_index = index + 1
            row.answered_count = len(graded)
            row.total_score = sum(float(item.score or 0) for item in graded)
            row.max_score = float(max(len(plan), 1) * 10)
            row.percentage_score = round(float(row.total_score) / row.max_score * 100, 2)
            row.tokens_used = int(row.tokens_used or 0) + tokens
            row.last_activity_at = now()
            started_at = as_utc(row.started_at)
            row.duration_seconds = (
                max(0, int((now() - started_at).total_seconds()))
                if started_at
                else elapsed
            )
            if completed:
                row.status = "completed"
                row.completed_at = now()
                row.transcript = "\n\n".join(
                    f"Q{item.question_index + 1}: {item.question_text}\nA: {item.answer_text or ''}"
                    for item in all_answers
                )
                average = float(row.total_score) / max(len(graded), 1)
                row.readiness_score = round(average * 10, 1)
                interview_strengths, interview_improvements = interview_summary_points(all_answers)
                if completion_summary:
                    row.readiness_score = float(
                        completion_summary.get("readiness", row.readiness_score)
                    )
                    row.summary_feedback = str(completion_summary.get("summary", ""))[:2000]
                    row.strengths = completion_summary.get("strengths") or interview_strengths
                    row.improvements = completion_summary.get("improvements") or interview_improvements
                    row.rubric_breakdown = {
                        "items": completion_summary.get("rubric") or []
                    }
                else:
                    row.summary_feedback = (
                        "You completed the interview. Review each answer and repeat the areas "
                        "that need clearer evidence."
                    )
                    row.strengths = interview_strengths
                    row.improvements = interview_improvements
                    row.rubric_breakdown = {
                        "items": [
                            {
                                "criterion": item,
                                "score": round(average, 1),
                                "max_score": 10,
                                "comment": "Keep developing this area.",
                            }
                            for item in RUBRICS["interview"]
                        ]
                    }
                gamification = award_completion(
                    session,
                    user["id"],
                    "interview",
                    float(row.percentage_score or 0),
                    row.language,
                )
            responses = session.scalars(
                select(SessionResponse)
                .where(SessionResponse.session_id == row.id)
                .order_by(SessionResponse.question_index)
            ).all()
            view = session_view(row, responses)
            return {
                "success": True,
                "session": view,
                "interviewer_reply": str(turn.get("reply", "")),
                "follow_up": should_follow_up,
                "completed": completed,
                "provider": turn.get("provider", "local-interview-fallback"),
                "gamification": gamification,
            }
    except Exception:
        _release_interview_turn_claim(user["id"], public_id, claim_token)
        raise


@app.patch("/api/practice/{public_id}")
def change_practice_status(
    public_id: str, payload: SessionStatusRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    target = payload.status
    if target not in {"in_progress", "paused", "abandoned"}:
        raise HTTPException(422, "Unknown session state.")
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        current = row.status
        if target == current:
            return {"message": "Session updated."}
        if current in {"completed", "failed", "abandoned"}:
            raise HTTPException(409, f"A {current} session cannot be reopened.")
        allowed = (
            (current == "in_progress" and target in {"paused", "abandoned"})
            or (current == "paused" and target in {"in_progress", "abandoned"})
            or (current == "created" and target in {"in_progress", "abandoned"})
        )
        if not allowed:
            raise HTTPException(409, f"Cannot move a {current} session to {target}.")

        config = dict(row.config or {})
        if row.mode == "interview":
            if _interview_turn_claim_is_active(config):
                raise HTTPException(
                    409,
                    "Wait for the interview response to finish before changing its status.",
                )
            config["tab_switch_count"] = payload.tab_switch_count
        values: dict[str, Any] = {
            "config": config,
            "status": target,
            "last_activity_at": now(),
            "state_version": PracticeSession.state_version + 1,
        }
        started_at = as_utc(row.started_at)
        if target in {"paused", "abandoned"} and started_at:
            values["duration_seconds"] = max(
                0, int((now() - started_at).total_seconds())
            )
        changed = session.execute(
            update(PracticeSession)
            .where(
                PracticeSession.id == row.id,
                PracticeSession.user_id == user["id"],
                PracticeSession.status == current,
                PracticeSession.state_version == int(row.state_version or 0),
            )
            .values(**values)
        )
        if changed.rowcount != 1:
            raise HTTPException(409, "This session changed. Refresh and try again.")
    return {"message": "Session updated."}


def _live_session_view(row: PracticeSession) -> dict[str, Any]:
    view = session_view(row)
    view["live_state"] = (row.config or {}).get("live_state") or {}
    return view


def _live_names(value: Any) -> list[str]:
    if isinstance(value, list):
        names = [str(item).strip()[:60] for item in value if str(item).strip()]
    else:
        raw = str(value or "").strip()
        if raw.isdigit():
            count = max(2, min(4, int(raw)))
            names = ["Maya", "Arjun", "Zoya", "Kabir"][:count]
        else:
            names = [item.strip()[:60] for item in raw.split(",") if item.strip()]
    return (names or ["Maya", "Arjun", "Zoya", "Kabir"])[:4]


@app.get("/api/debate/formats")
def debate_formats() -> list[dict[str, Any]]:
    return [
        {"id": "british_parliamentary", "name": "British Parliamentary", "description": "Four teams, comparative case-building and clash.", "speech_seconds": 420, "poi_allowed": True, "teams": 4},
        {"id": "public_forum", "name": "Public Forum", "description": "Direct rebuttal, weighing and accessible persuasion.", "speech_seconds": 240, "poi_allowed": False, "teams": 2},
        {"id": "oxford_style", "name": "Oxford-style", "description": "A focused proposition-versus-opposition motion.", "speech_seconds": 300, "poi_allowed": False, "teams": 2},
        {"id": "asian_parliamentary", "name": "Asian Parliamentary", "description": "A three-team format with substantive speeches and reply speeches.", "speech_seconds": 420, "poi_allowed": True, "teams": 3},
        {"id": "worlds_schools", "name": "Worlds Schools", "description": "A proposition-versus-opposition format built around clear signposting and examples.", "speech_seconds": 480, "poi_allowed": True, "teams": 2},
    ]


@app.get("/api/debate/scale")
def debate_scale() -> dict[str, Any]:
    """Expose the prior iteration's WUDC scale for reports and tutorials."""
    return WUDC_SCALE


@app.post("/api/live/debate/start")
def start_live_debate(
    payload: DebateStartRequest,
    user: dict[str, Any] = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    motion = (payload.motion or payload.topic).strip()
    if len(motion) < 4:
        raise HTTPException(422, "Give the debate a motion to argue.")
    side = payload.side
    language = payload.language
    fmt = payload.format
    key_hash = _idempotency_hash(idempotency_key)
    with db() as session:
        existing = _existing_idempotent_session(session, user["id"], key_hash)
        if existing:
            view = _live_session_view(existing)
            provider_info = user_safe_provider_status(
                (view["live_state"] or {}).get("provider") or existing.model_name or existing.provider
            )
            return {
                "success": True,
                "public_id": existing.public_id,
                "state": view["live_state"],
                "session": view,
                "idempotent": True,
                **{f"provider_{key}": value for key, value in provider_info.items() if key != "provider"},
            }
    difficulty = payload.difficulty
    ai_side = "opposition" if side == "proposition" else "proposition"
    state: dict[str, Any] = {
        "kind": "debate", "motion": motion, "format": fmt, "difficulty": difficulty,
        "user_side": side, "ai_side": ai_side,
        "language": language if language in LANGUAGES else "en",
        "speeches": [], "pois": [], "round": 1, "status": "in_progress", "last_feedback": None,
        "provider": "groq" if provider_status()["groq"] else "local-debate",
    }
    if side == "opposition":
        opening, detected, opening_provider = _await_on_application_loop(debate_turn(
            motion, ai_side, "Opening speech for the proposition.", [], language=state["language"],
        ))
        state["language"] = detected
        state["provider"] = opening_provider
        state["speeches"].append({"speaker": "opponent", "type": "opening", "content": opening})
    # H10: guard state size before first persist
    _guard_live_state_size(state)
    with db() as session:
        row = PracticeSession(
            public_id=str(uuid4()), user_id=user["id"], mode="debate",
            title=f"Live debate · {motion[:150]}", status="in_progress",
            topic=motion, difficulty=difficulty if difficulty in {"beginner", "intermediate", "advanced", "expert"} else "intermediate",
            language=state["language"], question_count=0, max_score=100,
            config={
                "side": side,
                "debate_format": fmt,
                "live_state": state,
                **({"idempotency_key_hash": key_hash} if key_hash else {}),
            },
            started_at=now(), last_activity_at=now(),
            provider="groq" if provider_status()["groq"] else "local",
            model_name=provider_status()["groq_model"] if provider_status()["groq"] else "local-debate",
        )
        session.add(row); session.flush()
        view = _live_session_view(row)
        public_id = row.public_id
    provider_info = user_safe_provider_status((view["live_state"] or {}).get("provider"))
    return {
        "success": True,
        "public_id": public_id,
        "state": view["live_state"],
        "session": view,
        **{f"provider_{key}": value for key, value in provider_info.items() if key != "provider"},
    }


@app.post("/api/live/debate/speech")
def live_debate_speech(
    payload: DebateSpeechRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    public_id = payload.public_id or payload.session_id
    if not public_id:
        raise HTTPException(422, "A live room id is required.")
    speech = (payload.speech or payload.answer).strip()
    if len(speech) < 20:
        raise HTTPException(422, "Write at least 20 characters for an assessed speech.")
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        state = dict((row.config or {}).get("live_state") or {})
        if state.get("kind") != "debate" or row.status != "in_progress":
            raise HTTPException(409, "That debate is no longer active.")
        expected_version = int(row.state_version or 0)
        state["speeches"] = list(state.get("speeches") or []) + [
            {
                "speaker": "user",
                "type": payload.type or payload.speech_type,
                "content": speech,
            }
        ]
        motion, ai_side, language = state.get("motion", row.topic), state.get("ai_side", "opposition"), state.get("language", "en")
    feedback = _await_on_application_loop(
        debate_evaluation(motion, state["speeches"], language=language)
    )
    opponent, detected, turn_provider = _await_on_application_loop(debate_turn(
        motion, ai_side, speech, state["speeches"], language=language
    ))
    state["language"] = detected
    state["provider"] = turn_provider
    state["last_feedback"] = feedback
    state["speeches"].append({"speaker": "opponent", "type": "rebuttal", "content": opponent})
    state["round"] = int(state.get("round", 1)) + 1
    # H10: guard before writing back
    _guard_live_state_size(state)
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        row = _commit_live_state(session, row, expected_version, state)
        view = _live_session_view(row)
    provider_info = user_safe_provider_status(turn_provider)
    return {
        "success": True,
        "feedback": feedback,
        "ai_response": opponent,
        "state": view["live_state"],
        "session": view,
        **{f"provider_{key}": value for key, value in provider_info.items() if key != "provider"},
    }


@app.post("/api/live/debate/poi")
def live_debate_poi(
    payload: DebatePoiRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    public_id = payload.public_id or payload.session_id
    if not public_id:
        raise HTTPException(422, "A live room id is required.")
    poi = payload.poi
    if len(poi) < 4:
        raise HTTPException(422, "Write the point of information you want to raise.")
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        state = dict((row.config or {}).get("live_state") or {})
        if state.get("kind") != "debate" or row.status != "in_progress":
            raise HTTPException(409, "That debate is no longer active.")
        expected_version = int(row.state_version or 0)
        if state.get("format") not in {"british_parliamentary", "asian_parliamentary", "worlds_schools"}:
            raise HTTPException(422, "Points of information are not used in this debate format.")
        state["pois"] = list(state.get("pois") or []) + [{"speaker": "user", "content": poi}]
        speech_context = list(state.get("speeches") or [])
        motion, ai_side, language = state.get("motion", row.topic), state.get("ai_side", "opposition"), state.get("language", "en")
    response, detected, turn_provider = _await_on_application_loop(debate_turn(
        motion, ai_side, f"Point of information: {poi}", speech_context, language=language
    ))
    state["language"] = detected
    state["provider"] = turn_provider
    accepted = not any(term in response.lower()[:160] for term in ("not accepted", "cannot take", "no,"))
    decision = "ACCEPT" if accepted else "REJECT"
    state["pois"][-1] = {**state["pois"][-1], "decision": decision}
    state["pois"].append({"speaker": "opponent", "type": "poi_response", "content": response})
    state["speeches"] = list(state.get("speeches") or []) + [
        {"speaker": "user", "type": "poi", "decision": decision, "content": poi},
        {"speaker": "opponent", "type": "poi_response", "content": response},
    ]
    _guard_live_state_size(state)
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        row = _commit_live_state(session, row, expected_version, state)
        view = _live_session_view(row)
    provider_info = user_safe_provider_status(turn_provider)
    return {
        "success": True,
        "accepted": accepted,
        "decision": decision,
        "ai_response": response,
        "state": view["live_state"],
        **{f"provider_{key}": value for key, value in provider_info.items() if key != "provider"},
    }


@app.post("/api/live/debate/end")
def end_live_debate(
    payload: LiveRoomRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    public_id = payload.public_id or payload.session_id
    if not public_id:
        raise HTTPException(422, "A live room id is required.")
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        state = dict((row.config or {}).get("live_state") or {})
        if state.get("kind") != "debate":
            raise HTTPException(409, "That is not a live debate.")
        # H5/H6: idempotent completion — return cached result if already done
        if row.status == "completed":
            completed_state = (row.config or {}).get("live_state") or {}
            provider_info = user_safe_provider_status(completed_state.get("provider"))
            return {"success": True, "evaluation": completed_state.get("final_evaluation") or {},
                    "session": _live_session_view(row),
                    "gamification": {"points_earned": 0, "new_achievements": [], "progress": {}},
                    **{f"provider_{key}": value for key, value in provider_info.items() if key != "provider"}}
        speeches = list(state.get("speeches") or [])
        motion, language = state.get("motion", row.topic), state.get("language", "en")
        expected_version = int(row.state_version or 0)
    evaluation = _await_on_application_loop(
        debate_evaluation(motion, speeches, language=language)
    )
    overall = max(0, min(100, int(evaluation.get("overall_score", 60))))
    rubric_keys = [
        ("Argument quality", "argument_quality"), ("Evidence use", "evidence_use"),
        ("Reasoning", "reasoning"), ("Strategic positioning", "strategic_positioning"),
    ]
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        # H5: re-check after async work — another request may have already completed it
        if row.status == "completed":
            completed_state = (row.config or {}).get("live_state") or {}
            provider_info = user_safe_provider_status(completed_state.get("provider"))
            return {"success": True, "evaluation": completed_state.get("final_evaluation") or {},
                    "session": _live_session_view(row),
                    "gamification": {"points_earned": 0, "new_achievements": [], "progress": {}},
                    **{f"provider_{key}": value for key, value in provider_info.items() if key != "provider"}}
        state["status"] = "completed"
        state["final_evaluation"] = evaluation
        _guard_live_state_size(state)
        row.config = {**(row.config or {}), "wudc": wudc_tier(wudc_score(overall))}
        rubric_breakdown = {"items": [
            {"criterion": label, "score": max(0, min(10, int(evaluation.get(key, 5)))), "max_score": 10, "comment": ""}
            for label, key in rubric_keys
        ]}
        row = _commit_live_state(
            session,
            row,
            expected_version,
            state,
            status="completed",
            fields={
                "completed_at": now(),
                "answered_count": len([x for x in speeches if x.get("speaker") == "user" and x.get("type") != "poi"]),
                "total_score": overall,
                "max_score": 100,
                "percentage_score": overall,
                "readiness_score": overall,
                "summary_feedback": str(evaluation.get("detailed_feedback") or evaluation.get("summary") or "Your debate report is ready.")[:2000],
                "strengths": evaluation.get("strengths") or [],
                "improvements": evaluation.get("areas_for_improvement") or evaluation.get("improvements") or [],
                "rubric_breakdown": rubric_breakdown,
                "transcript": "\n\n".join(f"{item.get('speaker', 'speaker')}: {item.get('content', '')}" for item in speeches),
            },
        )
        gamification = award_completion(session, user["id"], "debate", overall, language)
        view = _live_session_view(row)
    provider_info = user_safe_provider_status(state.get("provider"))
    return {
        "success": True,
        "evaluation": evaluation,
        "session": view,
        "gamification": gamification,
        **{f"provider_{key}": value for key, value in provider_info.items() if key != "provider"},
    }


@app.post("/api/live/gd/start")
def start_live_gd(
    payload: GroupDiscussionStartRequest,
    user: dict[str, Any] = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    topic = payload.topic
    language = payload.language
    names = _live_names(payload.participants)
    key_hash = _idempotency_hash(idempotency_key)
    with db() as session:
        existing = _existing_idempotent_session(session, user["id"], key_hash)
        if existing:
            view = _live_session_view(existing)
            provider_info = user_safe_provider_status(
                (view["live_state"] or {}).get("provider") or existing.model_name or existing.provider
            )
            return {
                "success": True,
                "public_id": existing.public_id,
                "state": view["live_state"],
                "session": view,
                "idempotent": True,
                **{f"provider_{key}": value for key, value in provider_info.items() if key != "provider"},
            }
    state: dict[str, Any] = {
        "kind": "group_discussion", "topic": topic,
        "difficulty": payload.difficulty,
        "language": language if language in LANGUAGES else "en",
        "participants": names,
        "messages": [{"speaker": "Moderator", "content": f"Discuss: {topic}. Make your opening contribution when ready."}],
        "status": "in_progress", "last_evaluation": None,
        "provider": "groq" if provider_status()["groq"] else "local-discussion",
    }
    openings, detected, opening_provider = _await_on_application_loop(gd_responses(
        topic,
        "Open the discussion with two distinct viewpoints.",
        names,
        state["messages"],
        language=state["language"],
    ))
    state["language"] = detected
    state["provider"] = opening_provider
    state["messages"].extend(openings)
    # H10: guard size before persist
    _guard_live_state_size(state)
    with db() as session:
        row = PracticeSession(
            public_id=str(uuid4()), user_id=user["id"], mode="group_discussion",
            title=f"Live discussion · {topic[:150]}", status="in_progress", topic=topic,
            difficulty=state["difficulty"] if state["difficulty"] in {"beginner", "intermediate", "advanced", "expert"} else "intermediate",
            language=state["language"], question_count=0, max_score=10,
            config={
                "participants": ", ".join(names),
                "live_state": state,
                **({"idempotency_key_hash": key_hash} if key_hash else {}),
            },
            started_at=now(), last_activity_at=now(),
            provider="groq" if provider_status()["groq"] else "local",
            model_name=provider_status()["groq_model"] if provider_status()["groq"] else "local-discussion",
        )
        session.add(row); session.flush()
        public_id = row.public_id
    provider_info = user_safe_provider_status(state.get("provider"))
    return {
        "success": True,
        "public_id": public_id,
        "state": state,
        **{f"provider_{key}": value for key, value in provider_info.items() if key != "provider"},
    }


@app.post("/api/live/gd/message")
def live_gd_message(
    payload: GroupDiscussionMessageRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    public_id = payload.public_id or payload.session_id
    if not public_id:
        raise HTTPException(422, "A live room id is required.")
    message = payload.message
    if len(message) < 10:
        raise HTTPException(422, "Write a little more so the panel can respond.")
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        state = dict((row.config or {}).get("live_state") or {})
        if state.get("kind") != "group_discussion" or row.status != "in_progress":
            raise HTTPException(409, "That discussion is no longer active.")
        expected_version = int(row.state_version or 0)
        state["messages"] = list(state.get("messages") or []) + [{"speaker": "You", "content": message}]
        topic, names, language = state.get("topic", row.topic), state.get("participants", []), state.get("language", "en")
    responses, detected, response_provider = _await_on_application_loop(gd_responses(
        topic, message, names, state["messages"], language=language
    ))
    state["language"] = detected
    state["provider"] = response_provider
    state["messages"].extend(responses)
    # H10: guard size before writing back
    _guard_live_state_size(state)
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        row = _commit_live_state(session, row, expected_version, state)
        view = _live_session_view(row)
    provider_info = user_safe_provider_status(response_provider)
    return {
        "success": True,
        "responses": responses,
        "state": view["live_state"],
        **{f"provider_{key}": value for key, value in provider_info.items() if key != "provider"},
    }


@app.post("/api/live/gd/end")
def end_live_gd(
    payload: LiveRoomRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    public_id = payload.public_id or payload.session_id
    if not public_id:
        raise HTTPException(422, "A live room id is required.")
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        state = dict((row.config or {}).get("live_state") or {})
        if state.get("kind") != "group_discussion":
            raise HTTPException(409, "That is not a live group discussion.")
        # H5/H6: idempotent completion
        if row.status == "completed":
            completed_state = (row.config or {}).get("live_state") or {}
            provider_info = user_safe_provider_status(completed_state.get("provider"))
            return {"success": True, "evaluation": completed_state.get("last_evaluation") or {},
                    "session": _live_session_view(row),
                    "gamification": {"points_earned": 0, "new_achievements": [], "progress": {}},
                    **{f"provider_{key}": value for key, value in provider_info.items() if key != "provider"}}
        messages, topic, language = list(state.get("messages") or []), state.get("topic", row.topic), state.get("language", "en")
        expected_version = int(row.state_version or 0)
    evaluation = _await_on_application_loop(
        gd_evaluation(topic, messages, language=language)
    )
    overall = max(1, min(10, int(evaluation.get("overall_score", 5))))
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        # H5: re-check after async work
        if row.status == "completed":
            completed_state = (row.config or {}).get("live_state") or {}
            provider_info = user_safe_provider_status(completed_state.get("provider"))
            return {"success": True, "evaluation": completed_state.get("last_evaluation") or {},
                    "session": _live_session_view(row),
                    "gamification": {"points_earned": 0, "new_achievements": [], "progress": {}},
                    **{f"provider_{key}": value for key, value in provider_info.items() if key != "provider"}}
        state["status"] = "completed"
        state["last_evaluation"] = evaluation
        _guard_live_state_size(state)
        rubric_breakdown = {"items": [
            {"criterion": label, "score": max(1, min(10, int(evaluation.get(key, 5)))), "max_score": 10, "comment": ""}
            for label, key in [
                ("Participation", "participation_score"), ("Content", "content_score"),
                ("Communication", "communication_score"), ("Leadership", "leadership_score"),
            ]
        ]}
        row = _commit_live_state(
            session,
            row,
            expected_version,
            state,
            status="completed",
            fields={
                "completed_at": now(),
                "answered_count": len([x for x in messages if x.get("speaker") == "You"]),
                "total_score": overall,
                "max_score": 10,
                "percentage_score": overall * 10,
                "readiness_score": overall * 10,
                "summary_feedback": str(evaluation.get("summary") or "Your discussion report is ready.")[:2000],
                "strengths": evaluation.get("strengths") or [],
                "improvements": evaluation.get("improvements") or [],
                "rubric_breakdown": rubric_breakdown,
                "transcript": "\n\n".join(f"{item.get('speaker', 'speaker')}: {item.get('content', '')}" for item in messages),
            },
        )
        gamification = award_completion(
            session, user["id"], "group_discussion", overall * 10, language,
            float(evaluation.get("leadership_score", overall)) * 10,
        )
        view = _live_session_view(row)
    provider_info = user_safe_provider_status(state.get("provider"))
    return {
        "success": True,
        "evaluation": evaluation,
        "session": view,
        "gamification": gamification,
        **{f"provider_{key}": value for key, value in provider_info.items() if key != "provider"},
    }


def _multiplayer_code() -> str:
    return secrets.token_hex(8).upper()


def _multiplayer_code_hash(value: str) -> str:
    return hashlib.sha256(value.strip().upper().encode("utf-8")).hexdigest()


def _multiplayer_event(
    *,
    speaker: str,
    content: str,
    role: str,
    kind: str,
    side: str = "",
    speech_type: str = "",
    event_id: str | None = None,
    reply_to: str | None = None,
) -> dict[str, Any]:
    return {
        "id": event_id or str(uuid4()),
        "speaker": speaker[:80],
        "content": content[:5000],
        "role": role,
        "kind": kind,
        "side": side,
        "speech_type": speech_type,
        "reply_to": reply_to,
        "created_at": now().isoformat(),
    }


def _multiplayer_room_row(session: Session, public_id: str) -> MultiplayerRoom:
    room = session.scalar(
        select(MultiplayerRoom).where(MultiplayerRoom.public_id == public_id)
    )
    if not room:
        raise HTTPException(404, "That shared room could not be found.")
    return room


def _multiplayer_member(
    session: Session, room_id: int, user_id: int
) -> MultiplayerRoomMember:
    member = session.scalar(
        select(MultiplayerRoomMember).where(
            MultiplayerRoomMember.room_id == room_id,
            MultiplayerRoomMember.user_id == user_id,
            MultiplayerRoomMember.is_active.is_(True),
        )
    )
    if not member:
        raise HTTPException(404, "Join this room before opening its live transcript.")
    return member


def _multiplayer_is_online(member: MultiplayerRoomMember) -> bool:
    last_seen = as_utc(member.last_seen_at)
    return bool(
        member.is_active
        and last_seen
        and last_seen >= now() - dt.timedelta(seconds=45)
    )


def _multiplayer_room_view(
    session: Session,
    room: MultiplayerRoom,
    user_id: int,
) -> dict[str, Any]:
    members = session.scalars(
        select(MultiplayerRoomMember)
        .where(
            MultiplayerRoomMember.room_id == room.id,
            MultiplayerRoomMember.is_active.is_(True),
        )
        .order_by(MultiplayerRoomMember.joined_at, MultiplayerRoomMember.id)
    ).all()
    counts = {"proposition": 0, "opposition": 0}
    participant_views = []
    for member in members:
        online = _multiplayer_is_online(member)
        if online and member.side in counts:
            counts[member.side] += 1
        participant_views.append({
            "display_name": member.display_name,
            "side": member.side,
            "is_host": bool(member.is_host),
            "is_self": member.user_id == user_id,
            "online": online,
        })
    state = dict(room.state or {})
    return {
        "public_id": room.public_id,
        "mode": room.mode,
        "visibility": room.visibility,
        "title": room.title,
        "topic": room.topic,
        "debate_format": room.debate_format,
        "difficulty": room.difficulty,
        "language": room.language,
        "status": room.status,
        "ai_enabled": bool(room.ai_enabled),
        "ai_side": room.ai_side,
        "max_participants": int(room.max_participants),
        "participant_count": sum(1 for item in participant_views if item["online"]),
        "side_counts": counts,
        "participants": participant_views,
        "events": state.get("events") or [],
        "evaluation": state.get("evaluation"),
        "state_version": int(room.state_version or 0),
        "is_host": room.host_user_id == user_id,
        "created_at": room.created_at.isoformat() if room.created_at else None,
        "last_activity_at": room.last_activity_at.isoformat() if room.last_activity_at else None,
    }


def _commit_multiplayer_state(
    session: Session,
    room: MultiplayerRoom,
    expected_version: int,
    state: dict[str, Any],
    *,
    status: str | None = None,
) -> MultiplayerRoom:
    _guard_live_state_size(state)
    values: dict[str, Any] = {
        "state": state,
        "state_version": MultiplayerRoom.state_version + 1,
        "last_activity_at": now(),
        "updated_at": now(),
    }
    criteria = [
        MultiplayerRoom.id == room.id,
        MultiplayerRoom.state_version == expected_version,
        MultiplayerRoom.status == "in_progress",
    ]
    if status is not None:
        values["status"] = status
        if status in {"completed", "cancelled"}:
            values["ended_at"] = now()
    result = session.execute(
        update(MultiplayerRoom).where(*criteria).values(**values)
    )
    if result.rowcount != 1:
        raise HTTPException(
            409,
            "This shared room changed while your request was being processed. Refresh and try again.",
        )
    updated = session.scalar(
        select(MultiplayerRoom)
        .where(MultiplayerRoom.id == room.id)
        .execution_options(populate_existing=True)
    )
    if not updated:
        raise HTTPException(404, "That shared room is no longer available.")
    return updated


def _balanced_multiplayer_side(
    session: Session, room_id: int, *, exclude_user_id: int | None = None
) -> str:
    members = session.scalars(
        select(MultiplayerRoomMember).where(
            MultiplayerRoomMember.room_id == room_id,
            MultiplayerRoomMember.is_active.is_(True),
            MultiplayerRoomMember.side.in_(("proposition", "opposition")),
        )
    ).all()
    counts = {"proposition": 0, "opposition": 0}
    for member in members:
        if member.user_id != exclude_user_id and _multiplayer_is_online(member):
            counts[member.side] += 1
    if counts["proposition"] < counts["opposition"]:
        return "proposition"
    if counts["opposition"] < counts["proposition"]:
        return "opposition"
    return "proposition"


def _public_multiplayer_room_view(
    session: Session, room: MultiplayerRoom
) -> dict[str, Any]:
    members = session.scalars(
        select(MultiplayerRoomMember).where(
            MultiplayerRoomMember.room_id == room.id,
            MultiplayerRoomMember.is_active.is_(True),
        )
    ).all()
    online = [member for member in members if _multiplayer_is_online(member)]
    host = next((member for member in members if member.is_host), None)
    return {
        "public_id": room.public_id,
        "mode": room.mode,
        "title": room.title,
        "topic": room.topic,
        "debate_format": room.debate_format,
        "status": room.status,
        "ai_enabled": bool(room.ai_enabled),
        "ai_side": room.ai_side,
        "participant_count": len(online),
        "max_participants": int(room.max_participants),
        "side_counts": {
            "proposition": sum(1 for item in online if item.side == "proposition"),
            "opposition": sum(1 for item in online if item.side == "opposition"),
        },
        "host_name": host.display_name if host else "Room host",
        "created_at": room.created_at.isoformat() if room.created_at else None,
    }


@app.get("/api/multiplayer/rooms")
def list_multiplayer_rooms(
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with db() as session:
        rooms = session.scalars(
            select(MultiplayerRoom)
            .where(
                MultiplayerRoom.visibility == "public",
                MultiplayerRoom.status == "in_progress",
            )
            .order_by(MultiplayerRoom.updated_at.desc())
            .limit(50)
        ).all()
        return {
            "rooms": [_public_multiplayer_room_view(session, room) for room in rooms]
        }


@app.post("/api/multiplayer/rooms")
def create_multiplayer_room(
    payload: MultiplayerRoomCreateRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    topic = payload.topic.strip()
    title = payload.title.strip() or topic[:160]
    host_side = payload.side if payload.mode == "debate" else ""
    ai_enabled = bool(payload.ai_enabled)
    ai_side = (
        (payload.ai_side or ("opposition" if host_side == "proposition" else "proposition"))
        if payload.mode == "debate" and ai_enabled
        else None
    )
    host_name = str(user.get("full_name") or user.get("email") or "Room host").strip()[:80]
    language = payload.language if payload.language in LANGUAGES else "en"
    event_list = [
        _multiplayer_event(
            speaker="Room",
            content=f"{host_name} opened the room.",
            role="system",
            kind="system",
        )
    ]

    if payload.mode == "debate" and ai_enabled and ai_side:
        opening, language, _ = _await_on_application_loop(debate_turn(
            topic,
            ai_side,
            f"Opening speech for the {ai_side} side.",
            [],
            language=language,
        ))
        event_list.append(_multiplayer_event(
            speaker="PrepSage AI",
            content=opening,
            role="ai",
            kind="speech",
            side=ai_side,
            speech_type="constructive",
        ))
    elif payload.mode == "group_discussion" and ai_enabled:
        responses, language, _ = _await_on_application_loop(gd_responses(
            topic,
            "Open the discussion with two concise, distinct viewpoints.",
            ["Maya", "Arjun"],
            [{"speaker": "Room", "content": f"Discuss: {topic}"}],
            language=language,
        ))
        event_list.extend(
            _multiplayer_event(
                speaker=item.get("speaker", "Panel"),
                content=item.get("content", ""),
                role="ai",
                kind="contribution",
            )
            for item in responses
        )

    room_state = {"events": event_list, "evaluation": None}
    _guard_live_state_size(room_state)
    invite_code = _multiplayer_code() if payload.visibility == "private" else None
    with db() as session:
        room = MultiplayerRoom(
            public_id=str(uuid4()),
            host_user_id=user["id"],
            invite_code_hash=_multiplayer_code_hash(invite_code) if invite_code else None,
            mode=payload.mode,
            visibility=payload.visibility,
            title=title,
            topic=topic,
            debate_format=payload.debate_format,
            difficulty=payload.difficulty,
            language=language,
            ai_enabled=ai_enabled,
            ai_side=ai_side,
            max_participants=payload.max_participants,
            status="in_progress",
            state=room_state,
            state_version=0,
            started_at=now(),
            last_activity_at=now(),
        )
        session.add(room)
        session.flush()
        session.add(MultiplayerRoomMember(
            room_id=room.id,
            user_id=user["id"],
            display_name=host_name,
            side=host_side,
            is_host=True,
            is_active=True,
            joined_at=now(),
            last_seen_at=now(),
        ))
        session.flush()
        view = _multiplayer_room_view(session, room, user["id"])
    return {
        "success": True,
        "room": view,
        "invite_code": invite_code,
    }


@app.post("/api/multiplayer/rooms/join")
def join_multiplayer_room(
    payload: MultiplayerRoomJoinRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if bool(payload.public_id) == bool(payload.invite_code):
        raise HTTPException(422, "Provide either a public room id or a private invite code.")
    with db() as session:
        if payload.invite_code:
            room = session.scalar(
                select(MultiplayerRoom).where(
                    MultiplayerRoom.invite_code_hash
                    == _multiplayer_code_hash(payload.invite_code)
                )
            )
            if not room:
                raise HTTPException(404, "That private invite code is not valid.")
        else:
            room = session.scalar(
                select(MultiplayerRoom).where(
                    MultiplayerRoom.public_id == payload.public_id,
                    MultiplayerRoom.visibility == "public",
                )
            )
            if not room:
                raise HTTPException(404, "That public room could not be found.")
        if room.status != "in_progress":
            raise HTTPException(409, "That room has already ended.")
        members = session.scalars(
            select(MultiplayerRoomMember).where(
                MultiplayerRoomMember.room_id == room.id,
                MultiplayerRoomMember.is_active.is_(True),
            )
        ).all()
        member = next((item for item in members if item.user_id == user["id"]), None)
        if member and member.is_active:
            member.last_seen_at = now()
            view = _multiplayer_room_view(session, room, user["id"])
            return {"success": True, "room": view, "rejoined": True}

        online_members = [item for item in members if _multiplayer_is_online(item)]
        if len(online_members) >= int(room.max_participants):
            raise HTTPException(409, "That room is full.")
        side = ""
        if room.mode == "debate":
            side = payload.side or _balanced_multiplayer_side(session, room.id)
        display_name = (
            payload.display_name.strip()
            or user.get("full_name")
            or user.get("email")
            or "Participant"
        )[:80]
        if member:
            member.display_name = display_name
            member.side = side
            member.is_active = True
            member.joined_at = now()
            member.last_seen_at = now()
        else:
            member = MultiplayerRoomMember(
                room_id=room.id,
                user_id=user["id"],
                display_name=display_name,
                side=side,
                is_host=False,
                is_active=True,
                joined_at=now(),
                last_seen_at=now(),
            )
            session.add(member)
            session.flush()
        state = dict(room.state or {})
        state["events"] = list(state.get("events") or []) + [
            _multiplayer_event(
                speaker="Room",
                content=(
                    f"{display_name} joined on {side}."
                    if side
                    else f"{display_name} joined the discussion."
                ),
                role="system",
                kind="system",
            )
        ]
        room = _commit_multiplayer_state(
            session, room, int(room.state_version or 0), state
        )
        view = _multiplayer_room_view(session, room, user["id"])
    return {"success": True, "room": view, "rejoined": False}


@app.get("/api/multiplayer/rooms/{public_id}")
def get_multiplayer_room(
    public_id: str,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with db() as session:
        room = _multiplayer_room_row(session, public_id)
        _multiplayer_member(session, room.id, user["id"])
        return {"room": _multiplayer_room_view(session, room, user["id"])}


@app.post("/api/multiplayer/rooms/{public_id}/heartbeat")
def multiplayer_room_heartbeat(
    public_id: str,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with db() as session:
        room = _multiplayer_room_row(session, public_id)
        member = _multiplayer_member(session, room.id, user["id"])
        member.last_seen_at = now()
    return {"success": True}


@app.post("/api/multiplayer/rooms/{public_id}/turn")
def send_multiplayer_room_turn(
    public_id: str,
    payload: MultiplayerRoomTurnRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    content = payload.message.strip()
    if len(content) < 4:
        raise HTTPException(422, "Write at least four characters for your turn.")
    with db() as session:
        room = _multiplayer_room_row(session, public_id)
        member = _multiplayer_member(session, room.id, user["id"])
        if room.status != "in_progress":
            raise HTTPException(409, "That room has already ended.")
        state = dict(room.state or {})
        events = list(state.get("events") or [])
        if any(item.get("id") == payload.request_id for item in events):
            return {"success": True, "room": _multiplayer_room_view(session, room, user["id"]), "duplicate": True}
        event = _multiplayer_event(
            event_id=payload.request_id,
            speaker=member.display_name,
            content=content,
            role="participant",
            kind="speech" if room.mode == "debate" else "contribution",
            side=member.side,
            speech_type=payload.speech_type if room.mode == "debate" else "",
        )
        state["events"] = events + [event]
        expected_version = int(room.state_version or 0)
        ai_enabled = bool(room.ai_enabled)
        ai_side = room.ai_side
        topic, mode, language = room.topic, room.mode, room.language
        room_format = room.debate_format
        submitted_side = member.side
        room = _commit_multiplayer_state(
            session, room, expected_version, state
        )

    ai_events: list[dict[str, Any]] = []
    if ai_enabled and mode == "debate" and ai_side and submitted_side != ai_side:
        speeches = [
            {
                "speaker": item.get("speaker", "speaker"),
                "type": item.get("speech_type") or "constructive",
                "content": item.get("content", ""),
            }
            for item in state["events"]
            if item.get("kind") == "speech"
        ]
        response, _, _ = _await_on_application_loop(debate_turn(
            topic, ai_side, content, speeches, language=language
        ))
        ai_events.append(_multiplayer_event(
            speaker="PrepSage AI",
            content=response,
            role="ai",
            kind="speech",
            side=ai_side,
            speech_type="rebuttal",
            reply_to=payload.request_id,
        ))
    elif ai_enabled and mode == "group_discussion":
        panel_names = ["Maya", "Arjun"]
        responses, _, _ = _await_on_application_loop(gd_responses(
            topic,
            content,
            panel_names,
            [
                {"speaker": item.get("speaker", "speaker"), "content": item.get("content", "")}
                for item in state["events"]
            ],
            language=language,
        ))
        ai_events.extend(
            _multiplayer_event(
                speaker=item.get("speaker", "Panel"),
                content=item.get("content", ""),
                role="ai",
                kind="contribution",
                reply_to=payload.request_id,
            )
            for item in responses
        )

    if ai_events:
        for _ in range(4):
            try:
                with db() as session:
                    room = _multiplayer_room_row(session, public_id)
                    if room.status != "in_progress":
                        break
                    current_state = dict(room.state or {})
                    existing_ids = {
                        item.get("id") for item in current_state.get("events") or []
                    }
                    additions = [
                        item for item in ai_events if item.get("id") not in existing_ids
                    ]
                    if not additions:
                        break
                    current_state["events"] = list(
                        current_state.get("events") or []
                    ) + additions
                    _commit_multiplayer_state(
                        session,
                        room,
                        int(room.state_version or 0),
                        current_state,
                    )
                break
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise

    with db() as session:
        room = _multiplayer_room_row(session, public_id)
        _multiplayer_member(session, room.id, user["id"])
        view = _multiplayer_room_view(session, room, user["id"])
    return {"success": True, "room": view}


@app.post("/api/multiplayer/rooms/{public_id}/leave")
def leave_multiplayer_room(
    public_id: str,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with db() as session:
        room = _multiplayer_room_row(session, public_id)
        member = _multiplayer_member(session, room.id, user["id"])
        if member.is_host and room.status == "in_progress":
            raise HTTPException(409, "The host must finish the room before leaving.")
        member.is_active = False
        if room.status == "in_progress":
            state = dict(room.state or {})
            state["events"] = list(state.get("events") or []) + [
                _multiplayer_event(
                    speaker="Room",
                    content=f"{member.display_name} left the room.",
                    role="system",
                    kind="system",
                )
            ]
            _commit_multiplayer_state(
                session, room, int(room.state_version or 0), state
            )
    return {"success": True}


@app.post("/api/multiplayer/rooms/{public_id}/finish")
def finish_multiplayer_room(
    public_id: str,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with db() as session:
        room = _multiplayer_room_row(session, public_id)
        member = _multiplayer_member(session, room.id, user["id"])
        if not member.is_host:
            raise HTTPException(403, "Only the room host can finish this room.")
        if room.status == "completed":
            return {
                "success": True,
                "room": _multiplayer_room_view(session, room, user["id"]),
            }
        if room.status != "in_progress":
            raise HTTPException(409, "That room cannot be finished.")
        state = dict(room.state or {})
        events = list(state.get("events") or [])
        topic, mode, language = room.topic, room.mode, room.language
        expected_version = int(room.state_version or 0)

    if mode == "debate":
        speeches = [
            {
                "speaker": "user" if item.get("side") == "proposition" else "opponent",
                "type": item.get("speech_type") or "constructive",
                "content": f"{item.get('speaker', 'Speaker')} ({item.get('side', '')}): {item.get('content', '')}",
            }
            for item in events
            if item.get("kind") == "speech"
        ]
        evaluation = _await_on_application_loop(
            debate_evaluation(topic, speeches, language=language)
        )
    else:
        messages = [
            {
                "speaker": "You" if item.get("role") == "participant" else item.get("speaker", "Panel"),
                "content": (
                    f"{item.get('speaker', 'Participant')}: {item.get('content', '')}"
                    if item.get("role") == "participant"
                    else item.get("content", "")
                ),
            }
            for item in events
            if item.get("kind") == "contribution"
        ]
        evaluation = _await_on_application_loop(
            gd_evaluation(topic, messages, language=language)
        )

    state["evaluation"] = evaluation
    with db() as session:
        room = _multiplayer_room_row(session, public_id)
        if room.status == "completed":
            return {
                "success": True,
                "room": _multiplayer_room_view(session, room, user["id"]),
            }
        room = _commit_multiplayer_state(
            session, room, expected_version, state, status="completed"
        )
        view = _multiplayer_room_view(session, room, user["id"])
    return {"success": True, "room": view}


# --------------------------------------------------------------------------- #
# Legacy route compatibility
#
# The archived Flask client used these URLs directly. Keep them as thin
# adapters over the persisted rooms above so bookmarks and older integrations
# do not lose access when the client is upgraded.
# --------------------------------------------------------------------------- #

def _legacy_question(view: dict[str, Any]) -> dict[str, Any] | None:
    question = (view.get("questions") or [])[int(view.get("current_index", 0) or 0):]
    if not question:
        return None
    item = question[0]
    return {
        "question": item.get("prompt", ""),
        "prompt": item.get("prompt", ""),
        "category": item.get("focus", ""),
        "focus": item.get("focus", ""),
        "tips": item.get("guidance", ""),
        "guidance": item.get("guidance", ""),
    }


def _legacy_interview_state(view: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": view["public_id"],
        "public_id": view["public_id"],
        "role": view.get("role_target", ""),
        "interview_type": view.get("interview_type") or "mixed",
        "difficulty": view.get("difficulty", "intermediate"),
        "total_questions": view.get("question_count", 0),
        "answered_count": view.get("answered_count", 0),
        "current_question": _legacy_question(view),
        "language": view.get("language", "en"),
        "status": view.get("status", "in_progress"),
        "qa_pairs": [
            {"question": item["question"], "answer": item["answer"], "evaluation": {
                "score": item["score"], "feedback": item["feedback"],
                "strengths": item["strengths"], "improvements": item["improvements"],
            }}
            for item in view.get("responses", [])
        ],
    }


@app.post("/debate/start")
def legacy_debate_start(
    payload: LegacyDebateStartRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    side = payload.side
    if not side and payload.position:
        side = "proposition" if payload.position.lower() == "government" else payload.position.lower()
    modern_payload = DebateStartRequest(
        motion=payload.motion,
        topic=payload.topic,
        side=side if side in {"proposition", "opposition"} else "proposition",
        format=payload.format
        if payload.format in {
            "british_parliamentary",
            "public_forum",
            "oxford_style",
            "asian_parliamentary",
            "worlds_schools",
        }
        else "british_parliamentary",
        difficulty=payload.difficulty
        if payload.difficulty in {"beginner", "intermediate", "advanced", "expert"}
        else "intermediate",
        language=payload.language,
    )
    result = start_live_debate(modern_payload, user)
    state = {**result["state"], "session_id": result["public_id"]}
    return {"success": True, "session_id": result["public_id"], "debate": state, "state": state}


@app.post("/debate/speech")
def legacy_debate_speech(
    payload: DebateSpeechRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    result = live_debate_speech(payload, user)
    public_id = payload.public_id
    return {**result, "session_id": public_id, "debate": result["state"]}


@app.post("/debate/poi")
def legacy_debate_poi(
    payload: DebatePoiRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    result = live_debate_poi(payload, user)
    public_id = payload.public_id
    return {**result, "session_id": public_id, "debate": result["state"]}


@app.post("/debate/end")
def legacy_debate_end(
    payload: LegacySessionRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    public_id = payload.public_id or payload.session_id
    return {
        **end_live_debate(LiveRoomRequest(public_id=public_id), user),
        "session_id": public_id,
    }


@app.post("/gd/start")
def legacy_gd_start(
    payload: GroupDiscussionStartRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    result = start_live_gd(payload, user)
    state = {**result["state"], "session_id": result["public_id"]}
    return {"success": True, "session_id": result["public_id"], "state": state}


@app.post("/gd/message")
def legacy_gd_message(
    payload: GroupDiscussionMessageRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    result = live_gd_message(payload, user)
    public_id = payload.public_id
    return {**result, "session_id": public_id}


@app.post("/gd/end")
def legacy_gd_end(
    payload: LegacySessionRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    public_id = payload.public_id or payload.session_id
    return {
        **end_live_gd(LiveRoomRequest(public_id=public_id), user),
        "session_id": public_id,
    }


@app.post("/interview/start")
def legacy_interview_start(
    payload: LegacyInterviewStartRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    result = start_practice(PracticeStartRequest(**{
        "mode": "interview",
        "role_target": payload.role or payload.role_target,
        "company": payload.company,
        "interview_type": payload.interview_type,
        "difficulty": payload.difficulty,
        "language": payload.language,
        "question_count": payload.total_questions or payload.question_count or 5,
        "project_info": payload.project_info,
    }), user)
    view = get_practice(result["public_id"], user)
    state = _legacy_interview_state(view)
    return {"success": True, "session_id": result["public_id"], "question": state["current_question"], "state": state, "language": view["language"]}


@app.post("/interview/answer")
def legacy_interview_answer(
    payload: LegacyInterviewAnswerRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    public_id = payload.public_id or payload.session_id
    view = answer_practice(public_id, AnswerRequest(
        answer=payload.answer,
        source=payload.source,
        response_seconds=payload.response_seconds,
    ), user)
    latest = (view.get("responses") or [])[-1] if view.get("responses") else {}
    evaluation = {
        "score": latest.get("score", 0), "feedback": latest.get("feedback", ""),
        "strengths": latest.get("strengths", []), "improvements": latest.get("improvements", []),
    }
    return {"success": True, "evaluation": evaluation, "state": _legacy_interview_state(view),
            "session_id": public_id, "language": view.get("language", "en"),
            "points_earned": (view.get("gamification") or {}).get("points_earned", 0)}


@app.post("/interview/next")
def legacy_interview_next(
    payload: LegacySessionRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    public_id = payload.public_id or payload.session_id
    view = get_practice(public_id, user)
    question = _legacy_question(view)
    if not question:
        raise HTTPException(409, "All questions answered.")
    return {"success": True, "question": question, "state": _legacy_interview_state(view),
            "session_id": public_id, "language": view.get("language", "en")}


@app.post("/interview/end")
def legacy_interview_end(
    payload: LegacyInterviewEndRequest, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    public_id = payload.public_id or payload.session_id
    with db() as session:
        row = owned_session(session, user["id"], public_id)
        if row.mode != "interview":
            raise HTTPException(409, "This room is not an interview.")
        if row.status in {"abandoned", "failed"}:
            raise HTTPException(409, f"A {row.status} interview cannot be completed.")
        if row.status != "completed" and _interview_turn_claim_is_active(dict(row.config or {})):
            raise HTTPException(
                409,
                "Wait for the interview response to finish before ending the interview.",
            )
        expected_version = int(row.state_version or 0)
        initial_status = row.status
        responses = session.scalars(select(SessionResponse).where(SessionResponse.session_id == row.id).order_by(SessionResponse.question_index)).all()
        transcript = "\n\n".join(f"Q{item.question_index + 1}: {item.question_text}\nA: {item.answer_text or ''}" for item in responses)
        average = sum(float(item.score or 0) for item in responses if item.is_graded) / max(len([item for item in responses if item.is_graded]), 1)
        already_complete = row.status == "completed"
        current_language = row.language
        config = {
            **(row.config or {}),
            "role_target": row.role_target,
            "interview_type": (row.config or {}).get("interview_type", "mixed"),
            "difficulty": row.difficulty,
        }
        tab_switches = payload.tab_switch_count
    if is_configured():
        try:
            summary_model, tokens = _await_on_application_loop(summarise_session(
                mode="interview", config=config, transcript=transcript, average_score=average
            ))
            summary = summary_model.model_dump()
        except ProviderError:
            tokens = 0
            summary = {}
    else:
        tokens = 0
        summary = {}
    interview_strengths, interview_improvements = interview_summary_points(responses)
    if not summary:
        summary = {
            "readiness": round(average * 10, 1),
            "summary": "You completed an interview practice session. Review each answer and repeat the areas that need clearer evidence.",
            "strengths": interview_strengths,
            "improvements": interview_improvements,
            "rubric": [],
        }
    else:
        summary["strengths"] = summary.get("strengths") or interview_strengths
        summary["improvements"] = summary.get("improvements") or interview_improvements
    if not already_complete:
        with db() as session:
            row = owned_session(session, user["id"], public_id)
            if row.status in {"abandoned", "failed", "completed"}:
                raise HTTPException(409, f"This interview is now {row.status}.")
            active_claim = dict(row.config or {})
            if _interview_turn_claim_is_active(active_claim):
                raise HTTPException(
                    409,
                    "Wait for the interview response to finish before ending the interview.",
                )
            if row.status != initial_status:
                raise HTTPException(409, "This interview changed. Refresh and try again.")
            claimed = session.execute(
                update(PracticeSession)
                .where(
                    PracticeSession.id == row.id,
                    PracticeSession.user_id == user["id"],
                    PracticeSession.status.in_(["in_progress", "paused"]),
                    PracticeSession.state_version == expected_version,
                )
                .values(state_version=PracticeSession.state_version + 1)
            )
            if claimed.rowcount != 1:
                raise HTTPException(409, "This interview changed. Refresh and try again.")
            row = session.scalar(
                select(PracticeSession)
                .where(PracticeSession.id == row.id)
                .execution_options(populate_existing=True)
            )
            if not row:
                raise HTTPException(409, "This interview is no longer available.")
            row.status, row.completed_at, row.last_activity_at = "completed", now(), now()
            row.transcript, row.tokens_used = transcript, row.tokens_used + tokens
            updated_config = dict(row.config or {})
            updated_config["tab_switch_count"] = tab_switches
            row.config = updated_config
            started_at = as_utc(row.started_at)
            if started_at:
                row.duration_seconds = max(0, int((now() - started_at).total_seconds()))
            row.total_score = sum(float(item.score or 0) for item in responses if item.is_graded)
            row.max_score = max(float(len(row.question_plan or []) * 10), 1)
            row.percentage_score = round(float(row.total_score) / row.max_score * 100, 2)
            row.readiness_score = float(summary.get("readiness", row.percentage_score))
            row.summary_feedback = str(summary.get("summary", ""))[:2000]
            row.strengths, row.improvements = summary.get("strengths", []), summary.get("improvements", [])
            row.rubric_breakdown = {"items": summary.get("rubric", [])}
            gamification = award_completion(session, user["id"], "interview", row.percentage_score, current_language)
        points = gamification["points_earned"]
        achievements = gamification["new_achievements"]
    else:
        points, achievements = 0, []
    return {"success": True, "summary": summary, "points_earned": points,
            "new_achievements": achievements, "session_id": public_id, "language": current_language}


def admin_user(user: dict[str, Any] = None) -> dict[str, Any]:
    return require_admin(user)


@app.get("/api/admin/overview")
def admin_overview(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    admin_user(user)
    with db() as session:
        total = session.scalar(select(func.count(User.id))) or 0
        sessions = session.scalar(select(func.count(PracticeSession.id))) or 0
        completed = session.scalar(select(func.count(PracticeSession.id)).where(PracticeSession.status == "completed")) or 0
        return {"users": total, "sessions": sessions, "completed_sessions": completed,
                "failed_sessions": session.scalar(select(func.count(PracticeSession.id)).where(PracticeSession.status == "failed")) or 0,
                "content": session.scalar(select(func.count(PreparationContent.id))) or 0}


@app.get("/api/admin/provider-metrics")
def admin_provider_metrics(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """Secret-free provider reliability data for the operations console."""
    admin_user(user)
    return {"providers": provider_status(), "metrics": provider_snapshot()}


@app.get("/api/admin/learners")
def admin_learners(
    limit: int = 50,
    offset: int = 0,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_user(user)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    with db() as session:
        total = session.scalar(select(func.count(User.id))) or 0
        users = session.scalars(
            select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
        ).all()
        user_ids = [u.id for u in users]
        counts_map: dict[int, int] = {}
        if user_ids:
            counts_map = dict(session.execute(
                select(PracticeSession.user_id, func.count(PracticeSession.id))
                .where(PracticeSession.user_id.in_(user_ids))
                .group_by(PracticeSession.user_id)
            ).all())
        return {
            "learners": [
                {**public_user(row), "session_count": counts_map.get(row.id, 0)}
                for row in users
            ],
            "total": int(total),
            "limit": limit,
            "offset": offset,
        }


@app.patch("/api/admin/learners/{user_id}")
def admin_update_learner(
    user_id: int,
    payload: AdminLearnerUpdateRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_user(user)
    if payload.is_active is None and payload.role is None:
        raise HTTPException(422, "Provide an access or role change.")
    with db() as session:
        row = session.get(User, user_id)
        if not row: raise HTTPException(404, "Learner not found.")
        if payload.is_active is not None:
            row.is_active = payload.is_active
        if payload.role is not None:
            row.role = payload.role
        audit(session, "admin.learner_updated", actor=session.get(User, user["id"]), entity_type="app_user", entity_id=str(row.id), description="Learner access updated.")
    return {"message": "Learner updated."}


@app.get("/api/admin/content")
def admin_content(
    limit: int = 50,
    offset: int = 0,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_user(user)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    with db() as session:
        total = session.scalar(select(func.count(PreparationContent.id))) or 0
        rows = session.scalars(
            select(PreparationContent)
            .order_by(PreparationContent.updated_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return {
            "content": [
                {
                    "id": r.id, "mode": r.mode, "title": r.title, "prompt": r.prompt,
                    "guidance": r.guidance or "", "category": r.category, "topic": r.topic,
                    "difficulty": r.difficulty, "duration_seconds": r.expected_duration_seconds,
                    "rubric": r.rubric or {}, "tags": r.tags or [],
                    "is_active": r.is_active, "usage_count": r.usage_count,
                }
                for r in rows
            ],
            "total": int(total),
            "limit": limit,
            "offset": offset,
        }


@app.post("/api/admin/content")
@app.put("/api/admin/content/{content_id}")
def admin_save_content(
    payload: AdminContentRequest,
    content_id: int = 0,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_user(user)
    mode, title, prompt = payload.mode, payload.title, payload.prompt
    with db() as session:
        row = session.get(PreparationContent, content_id) if content_id else None
        if not row: row = PreparationContent(mode=mode, slug=f"{re.sub('[^a-z0-9]+', '-', title.lower()).strip('-')[:140]}-{secrets.token_hex(3)}", title=title, prompt=prompt); session.add(row)
        row.mode, row.title, row.prompt, row.guidance, row.category, row.topic = (
            mode,
            title,
            prompt,
            payload.guidance,
            payload.category,
            payload.topic,
        )
        row.difficulty = payload.difficulty
        row.expected_duration_seconds = payload.duration_seconds
        row.tags, row.rubric = payload.tags, payload.rubric
    return {"message": "Prompt saved."}


@app.patch("/api/admin/content/{content_id}")
def admin_toggle_content(
    content_id: int,
    payload: AdminContentToggleRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    admin_user(user)
    with db() as session:
        row = session.get(PreparationContent, content_id)
        if not row: raise HTTPException(404, "Prompt not found.")
        row.is_active = payload.is_active
    return {"message": "Prompt updated."}


@app.get("/api/admin/sessions")
def admin_sessions(
    limit: int = 100,
    offset: int = 0,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    # D4: paginated admin sessions — replaced unbounded limit(200)
    admin_user(user)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with db() as session:
        total = session.scalar(select(func.count(PracticeSession.id))) or 0
        rows = session.scalars(
            select(PracticeSession)
            .order_by(PracticeSession.created_at.desc())
            .limit(limit).offset(offset)
        ).all()
        # D4: avoid N+1 — fetch user emails in one query
        user_ids = list({row.user_id for row in rows})
        email_map: dict[int, str] = {}
        if user_ids:
            email_map = dict(session.execute(
                select(User.id, User.email).where(User.id.in_(user_ids))
            ).all())
        return {
            "sessions": [session_view(row) | {"learner_email": email_map.get(row.user_id, "")} for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


@app.get("/api/admin/audit")
def admin_audit(
    limit: int = 100,
    offset: int = 0,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    # D4: paginated audit log — replaced unbounded limit(200)
    admin_user(user)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with db() as session:
        total = session.scalar(select(func.count(AuditRecord.id))) or 0
        rows = session.scalars(
            select(AuditRecord)
            .order_by(AuditRecord.created_at.desc())
            .limit(limit).offset(offset)
        ).all()
        return {
            "records": [
                {
                    "created_at": r.created_at.strftime("%d %b %Y, %H:%M UTC"),
                    "actor_email": r.actor_email,
                    "action": r.action,
                    "outcome": r.outcome,
                    "severity": r.severity,
                    "description": r.description or "",
                }
                for r in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


_ADMIN_EXPORT_FIELDS: dict[str, list[str]] = {
    "learners": [
        "id", "email", "full_name", "role", "is_active", "is_verified",
        "headline", "institution", "field_of_study", "target_role", "timezone",
        "avatar_seed", "created_at", "last_login_at", "level", "total_points",
        "practice_streak", "last_practice_date", "level_progress", "session_count",
    ],
    "sessions": [
        "public_id", "mode", "mode_label", "title", "status", "status_label",
        "is_live", "live_contribution_count", "difficulty", "role_target", "topic",
        "company", "context_notes", "side", "debate_format", "rounds",
        "participants", "focus_area", "question_count", "current_index",
        "answered_count", "total_score", "max_score", "percentage_score",
        "readiness_score", "summary_feedback", "strengths", "improvements",
        "rubric", "transcript", "tokens_used", "model_name", "provider",
        "error_message", "provider_degraded", "provider_notice", "provider_pending",
        "duration_seconds", "duration_label", "created_label", "questions",
        "responses", "document_id", "language", "live_state", "interview_type",
        "project_info", "interviewer", "interview_minutes", "time_limit_seconds",
        "interview_messages", "tab_switch_count", "wudc", "is_resumable",
        "learner_email",
    ],
    "audit": [
        "created_at", "actor_email", "action", "outcome", "severity", "description",
    ],
}


def _admin_export_rows(kind: str, chunk_size: int = 500):
    """Yield every matching export row while keeping each DB read bounded."""
    offset = 0
    while True:
        with db() as session:
            if kind == "learners":
                rows = session.scalars(
                    select(User)
                    .order_by(User.created_at.desc(), User.id.desc())
                    .limit(chunk_size)
                    .offset(offset)
                ).all()
                user_ids = [row.id for row in rows]
                counts_map: dict[int, int] = {}
                if user_ids:
                    counts_map = dict(session.execute(
                        select(PracticeSession.user_id, func.count(PracticeSession.id))
                        .where(PracticeSession.user_id.in_(user_ids))
                        .group_by(PracticeSession.user_id)
                    ).all())
                batch = [
                    {**public_user(row), "session_count": counts_map.get(row.id, 0)}
                    for row in rows
                ]
            elif kind == "sessions":
                rows = session.scalars(
                    select(PracticeSession)
                    .order_by(PracticeSession.created_at.desc(), PracticeSession.id.desc())
                    .limit(chunk_size)
                    .offset(offset)
                ).all()
                user_ids = list({row.user_id for row in rows})
                email_map: dict[int, str] = {}
                if user_ids:
                    email_map = dict(session.execute(
                        select(User.id, User.email).where(User.id.in_(user_ids))
                    ).all())
                batch = [
                    session_view(row) | {"learner_email": email_map.get(row.user_id, "")}
                    for row in rows
                ]
            else:
                rows = session.scalars(
                    select(AuditRecord)
                    .order_by(AuditRecord.created_at.desc(), AuditRecord.id.desc())
                    .limit(chunk_size)
                    .offset(offset)
                ).all()
                batch = [
                    {
                        "created_at": r.created_at.strftime("%d %b %Y, %H:%M UTC"),
                        "actor_email": r.actor_email,
                        "action": r.action,
                        "outcome": r.outcome,
                        "severity": r.severity,
                        "description": r.description or "",
                    }
                    for r in rows
                ]
        if not batch:
            return
        yield from batch
        offset += len(batch)


def _safe_csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text_value = str(value)
    return f"'{text_value}" if text_value.startswith(("=", "+", "-", "@")) else text_value


@app.get("/api/admin/export/{kind}")
def admin_export(kind: str, user: dict[str, Any] = Depends(current_user)) -> StreamingResponse:
    admin_user(user)
    if kind not in _ADMIN_EXPORT_FIELDS:
        raise HTTPException(404, "Unknown export.")

    def stream_csv():
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=_ADMIN_EXPORT_FIELDS[kind],
            extrasaction="ignore",
        )
        writer.writeheader()
        yield output.getvalue()
        for row in _admin_export_rows(kind):
            output.seek(0)
            output.truncate(0)
            writer.writerow({
                key: _safe_csv_value(value)
                for key, value in row.items()
            })
            yield output.getvalue()

    return StreamingResponse(
        stream_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="prepsage-{kind}.csv"'},
    )


@app.get("/{path:path}", include_in_schema=False)
def client(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(404, "API route not found.")
    frontend_routes = {
        "",
        "login",
        "register",
        "home",
        "dashboard",
        "practice",
        "arena",
        "debate",
        "gd",
        "interview",
        "coach",
        "history",
        "account",
        "profile",
        "admin",
    }
    parts = [part for part in path.split("/") if part]
    is_dynamic_route = (
        len(parts) == 2
        and parts[0] in {"practice", "history"}
        and bool(parts[1])
    )
    if path in frontend_routes or is_dynamic_route:
        return FileResponse(ROOT / "static" / "index.html")
    raise HTTPException(404, "Frontend route not found.")
