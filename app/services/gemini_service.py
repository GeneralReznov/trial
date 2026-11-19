"""Gemini provider layer for PrepSage AI.

One shared client, bounded timeouts, capped input and output, retries only for
transient server failures, validated Pydantic structured output, and safe
user-facing error strings. No secret is ever logged or returned.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field, ValidationError

from app.services.key_rotator import KeyRotator
from app.utils.token_optimizer import compact_mapping, compact_text, compact_transcript

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
REQUEST_TIMEOUT_SECONDS = 55.0
MAX_RETRIES = 2
MAX_PROMPT_CHARS = 12_000
MAX_DOCUMENT_CHARS = 4_000
MAX_ANSWER_CHARS = 6_000
MAX_OUTPUT_TOKENS = 2_048

PROVIDER_UNAVAILABLE = (
    "The coaching engine is not configured on this server yet. "
    "Please contact your administrator."
)
PROVIDER_BUSY = (
    "The coaching engine is busy right now. Please try again in a moment."
)
PROVIDER_ERROR = (
    "We could not reach the coaching engine. Your progress has been saved — "
    "please try again."
)
PROVIDER_FORMAT_ERROR = (
    "The coaching engine returned an unexpected response. Please try again."
)

_client: genai.Client | None = None
_client_lock = threading.Lock()
_client_key: str | None = None
GEMINI_KEYS = KeyRotator(
    "GEMINI_API_KEYS",
    ("GOOGLE_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
)


# --------------------------------------------------------------------------- #
# Structured output schemas
# --------------------------------------------------------------------------- #


class GeneratedQuestion(BaseModel):
    prompt: str = Field(description="The question or prompt for the learner.")
    focus: str = Field(
        default="", description="Short label for what this probes."
    )
    guidance: str = Field(
        default="", description="One sentence of coaching guidance."
    )


class GeneratedPlan(BaseModel):
    title: str = Field(default="", description="Short title for the session.")
    questions: list[GeneratedQuestion] = Field(default_factory=list)


class RubricScore(BaseModel):
    criterion: str
    score: float = Field(ge=0, le=10)
    max_score: float = Field(default=10.0, ge=1, le=10)
    comment: str = Field(default="")


class AnswerEvaluation(BaseModel):
    score: float = Field(ge=0, le=10)
    feedback: str = Field(default="")
    strengths: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    rubric: list[RubricScore] = Field(default_factory=list)


class SessionSummary(BaseModel):
    readiness: float = Field(ge=0, le=100)
    summary: str = Field(default="")
    strengths: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    rubric: list[RubricScore] = Field(default_factory=list)


class TextGeneration(BaseModel):
    text: str = Field(description="The requested response.")


class ProviderError(Exception):
    """Raised with a safe, user-facing message."""

    def __init__(self, message: str, code: str = "provider_error") -> None:
        super().__init__(message)
        self.safe_message = message
        self.code = code


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


def model_name() -> str:
    return (os.getenv("GEMINI_MODEL") or "").strip() or DEFAULT_MODEL


def _api_key() -> str:
    return GEMINI_KEYS.current() or ""


def is_configured() -> bool:
    return GEMINI_KEYS.available


def _shared_client() -> genai.Client:
    """Return the process-wide client, creating it once."""
    global _client, _client_key
    key = _api_key()
    if not key:
        raise ProviderError(PROVIDER_UNAVAILABLE, "not_configured")
    if _client is not None and _client_key == key:
        return _client
    with _client_lock:
        if _client is None or _client_key != key:
            base_url = (os.getenv("GEMINI_BASE_URL") or "").strip()
            http_options = types.HttpOptions(
                base_url=base_url or None,
                timeout=int(REQUEST_TIMEOUT_SECONDS * 1000),
            )
            try:
                _client = genai.Client(api_key=key, http_options=http_options)
                _client_key = key
            except (errors.APIError, TypeError, ValueError) as exc:
                logger.warning("Gemini client could not be initialised: %s", type(exc).__name__)
                raise ProviderError(
                    PROVIDER_UNAVAILABLE, "client_init"
                ) from exc
    return _client


def _reset_client() -> None:
    """Discard the client bound to a failed key before rotating."""
    global _client, _client_key
    with _client_lock:
        _client = None
        _client_key = None


def _cap(text: str, limit: int) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n[truncated]"


async def _generate(
    *,
    system_instruction: str,
    prompt: str,
    schema: type[BaseModel],
    temperature: float,
) -> tuple[BaseModel, int]:
    """Single structured call with bounded timeout and transient-only retries."""
    client = _shared_client()
    config = types.GenerateContentConfig(
        system_instruction=_cap(system_instruction, 2_000),
        temperature=temperature,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        response_mime_type="application/json",
        response_schema=schema,
    )
    capped_prompt = _cap(prompt, MAX_PROMPT_CHARS)
    last_error: ProviderError | None = None

    key_rotations = 0
    max_attempts = max(MAX_RETRIES + 1, GEMINI_KEYS.count)
    for attempt in range(max_attempts):
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model_name(),
                    contents=capped_prompt,
                    config=config,
                ),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except errors.ServerError as exc:
            if GEMINI_KEYS.is_rotation_error(exc) and key_rotations < GEMINI_KEYS.count - 1:
                GEMINI_KEYS.rotate()
                key_rotations += 1
                _reset_client()
                continue
            logger.warning(
                "Gemini transient server failure (attempt %s).",
                attempt + 1,
            )
            last_error = ProviderError(PROVIDER_BUSY, "server_error")
            if attempt < max_attempts - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise last_error from exc
        except errors.APIError as exc:
            status = getattr(exc, "code", None) or getattr(exc, "status_code", None) or "api"
            if GEMINI_KEYS.is_rotation_error(exc) and key_rotations < GEMINI_KEYS.count - 1:
                GEMINI_KEYS.rotate()
                key_rotations += 1
                _reset_client()
                continue
            busy = status in {429, 500, 502, 503, 504, "429", "500", "502", "503", "504"} or "RESOURCE_EXHAUSTED" in str(exc)
            if busy:
                logger.warning("Gemini request temporarily unavailable (status %s).", status)
                raise ProviderError(PROVIDER_BUSY, "rate_limited") from exc
            logger.warning("Gemini API rejected a request (status %s).", status)
            raise ProviderError(PROVIDER_ERROR, "api_error") from exc
        except asyncio.TimeoutError as exc:
            logger.warning("Gemini call exceeded the bounded timeout.")
            raise ProviderError(PROVIDER_BUSY, "timeout") from exc
        except (errors.ClientError, TypeError, ValueError) as exc:
            logger.warning("Gemini request failed: %s", type(exc).__name__)
            raise ProviderError(PROVIDER_ERROR, "unexpected") from exc

        raw = (response.text or "").strip()
        tokens = 0
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            tokens = int(getattr(usage, "total_token_count", 0) or 0)
        try:
            return schema.model_validate_json(raw), tokens
        except ValidationError as exc:
            logger.warning("Gemini returned output failing schema validation.")
            last_error = ProviderError(PROVIDER_FORMAT_ERROR, "invalid_output")
            if attempt < MAX_RETRIES:
                continue
            raise last_error from exc

    raise last_error or ProviderError(PROVIDER_ERROR, "unknown")


async def generate_text(
    *,
    system_instruction: str,
    prompt: str,
    temperature: float = 0.4,
) -> tuple[str, int]:
    """Generate plain text through the shared, bounded Gemini client."""
    result, tokens = await _generate(
        system_instruction=system_instruction,
        prompt=prompt,
        schema=TextGeneration,
        temperature=temperature,
    )
    return result.text.strip(), tokens


# --------------------------------------------------------------------------- #
# Mode prompts
# --------------------------------------------------------------------------- #

MODE_SYSTEM = {
    "interview": (
        "You are a senior interview coach. You write realistic, role-specific"
        " interview questions and grade answers strictly but constructively."
    ),
    "group_discussion": (
        "You are a group-discussion moderator and assessor. You simulate a"
        " panel of peers debating a topic and assess a participant's"
        " contributions on content, clarity, initiative and teamwork."
    ),
    "debate": (
        "You are a WUDC-accredited debate adjudicator. You set motions and"
        " points of clash, and score speeches on matter, manner, method and"
        " role fulfilment."
    ),
    "coaching": (
        "You are a personal preparation coach. You ask reflective, practical"
        " coaching questions and give warm, specific, actionable feedback."
    ),
}

MODE_RUBRIC = {
    "interview": (
        "Relevance, Structure, Evidence and examples, Communication, Impact"
    ),
    "group_discussion": (
        "Content depth, Clarity, Initiative, Collaboration, Summarising"
    ),
    "debate": (
        "Matter, Manner, Method, Role fulfilment, Engagement with clash"
    ),
    "coaching": (
        "Self-awareness, Specificity, Action orientation, Reflection quality"
    ),
}


def _context_block(config: dict[str, str]) -> str:
    lines: list[str] = []
    for label, key in (
        ("Target role", "role_target"),
        ("Target company", "company"),
        ("Topic or motion", "topic"),
        ("Projects or experience", "project_info"),
        ("Interviewer", "interviewer"),
        ("Interview duration (minutes)", "interview_minutes"),
        ("Difficulty", "difficulty"),
        ("Debate side", "side"),
        ("Debate format", "debate_format"),
        ("Rounds", "rounds"),
        ("Participants", "participants"),
        ("Coaching focus", "focus_area"),
        ("Response language", "language"),
        ("Extra context", "context_notes"),
    ):
        value = str(config.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


async def generate_question_plan(
    *,
    mode: str,
    question_count: int,
    config: dict[str, str],
    document_text: str = "",
) -> tuple[GeneratedPlan, int]:
    """Generate the ordered prompts for a session."""
    system = MODE_SYSTEM.get(mode, MODE_SYSTEM["interview"])
    doc = compact_text(document_text, MAX_DOCUMENT_CHARS, preserve_ends=True)
    parts = [
        f"Create exactly {question_count} prompts for a {mode.replace('_', ' ')} rehearsal.",
        _context_block(config),
    ]
    if mode == "debate":
        parts.append(
            "Each prompt is one speech task for the learner's assigned side,"
            " advancing round by round and responding to the likely clash."
        )
    if mode == "group_discussion":
        parts.append(
            "Each prompt is a moderator turn that reports what other"
            " participants just argued and asks the learner to respond."
        )
    if doc:
        parts.append(
            "Ground the prompts in this candidate document context:\n" + doc
        )
    parts.append(
        "Prompts must be self-contained, escalate in difficulty, and never"
        " repeat one another. Keep each prompt under 60 words. Respond in the"
        " requested response language when it is not English."
    )
    plan, tokens = await _generate(
        system_instruction=system,
        prompt="\n\n".join(p for p in parts if p),
        schema=GeneratedPlan,
        temperature=0.9,
    )
    return plan, tokens  # type: ignore[return-value]


async def evaluate_answer(
    *,
    mode: str,
    question: str,
    answer: str,
    config: dict[str, str],
) -> tuple[AnswerEvaluation, int]:
    """Score a single answer out of 10 against the mode rubric."""
    system = MODE_SYSTEM.get(mode, MODE_SYSTEM["interview"])
    rubric = MODE_RUBRIC.get(mode, MODE_RUBRIC["interview"])
    prompt = "\n\n".join(
        p
        for p in (
            compact_mapping(config, 3000),
            f"Prompt given to the learner:\n{compact_text(question, 2_000)}",
            f"Learner's answer:\n{compact_text(answer, MAX_ANSWER_CHARS, preserve_ends=True)}",
            (
                f"Score the answer out of 10 overall and score each of these"
                f" criteria out of 10: {rubric}. Give at most three strengths"
                " and three improvements, each one short sentence. Feedback"
                " must be under 90 words and address the learner directly. Use"
                " the requested response language when it is not English."
            ),
        )
        if p
    )
    evaluation, tokens = await _generate(
        system_instruction=system,
        prompt=prompt,
        schema=AnswerEvaluation,
        temperature=0.3,
    )
    return evaluation, tokens  # type: ignore[return-value]


async def summarise_session(
    *,
    mode: str,
    config: dict[str, str],
    transcript: str,
    average_score: float,
) -> tuple[SessionSummary, int]:
    """Produce readiness, rubric breakdown and closing feedback."""
    system = MODE_SYSTEM.get(mode, MODE_SYSTEM["interview"])
    rubric = MODE_RUBRIC.get(mode, MODE_RUBRIC["interview"])
    prompt = "\n\n".join(
        p
        for p in (
            _context_block(config),
            f"Average per-answer score so far: {average_score:.1f} out of 10.",
            f"Full transcript:\n{compact_transcript(transcript, MAX_PROMPT_CHARS - 1_500)}",
            (
                "Give an overall readiness percentage from 0 to 100, a rubric"
                f" breakdown out of 10 for each of: {rubric}, three strengths,"
                " three improvements, and a closing summary under 120 words"
                " addressed to the learner."
            ),
        )
        if p
    )
    summary, tokens = await _generate(
        system_instruction=system,
        prompt=prompt,
        schema=SessionSummary,
        temperature=0.4,
    )
    return summary, tokens  # type: ignore[return-value]
