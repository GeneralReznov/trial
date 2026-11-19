"""Conversational and voice AI features for PrepSage.

This module keeps provider SDK calls out of the route layer. Calls are bounded,
secrets stay server-side, and every public helper has a deterministic fallback
so the rest of the product remains usable during a provider outage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from typing import Any

import groq

from app.services.gemini_service import (
    GEMINI_KEYS,
    ProviderError as GeminiProviderError,
    generate_text as gemini_generate_text,
    is_configured as gemini_is_configured,
)
from app.services.key_rotator import KeyRotator
from app.services.provider_observability import record_provider_event
from app.utils.token_optimizer import (
    compact_history,
    compact_mapping,
    compact_text,
    compact_transcript,
)


logger = logging.getLogger(__name__)


LANGUAGES: dict[str, dict[str, str]] = {
    "en": {"name": "English", "speech": "en-IN"},
    "hi": {"name": "हिंदी", "speech": "hi-IN"},
    "bn": {"name": "বাংলা", "speech": "bn-IN"},
    "ta": {"name": "தமிழ்", "speech": "ta-IN"},
    "te": {"name": "తెలుగు", "speech": "te-IN"},
    "mr": {"name": "मराठी", "speech": "mr-IN"},
    "gu": {"name": "ગુજરાતી", "speech": "gu-IN"},
    "kn": {"name": "ಕನ್ನಡ", "speech": "kn-IN"},
    "ml": {"name": "മലയാളം", "speech": "ml-IN"},
    "pa": {"name": "ਪੰਜਾਬੀ", "speech": "pa-IN"},
    "ur": {"name": "اردو", "speech": "ur-IN"},
    "or": {"name": "ଓଡ଼ିଆ", "speech": "or-IN"},
    "as": {"name": "অসমীয়া", "speech": "as-IN"},
    "ne": {"name": "नेपाली", "speech": "ne-NP"},
    "ar": {"name": "العربية", "speech": "ar-SA"},
    "fr": {"name": "Français", "speech": "fr-FR"},
    "de": {"name": "Deutsch", "speech": "de-DE"},
    "es": {"name": "Español", "speech": "es-ES"},
    "it": {"name": "Italiano", "speech": "it-IT"},
    "pt": {"name": "Português", "speech": "pt-BR"},
    "ja": {"name": "日本語", "speech": "ja-JP"},
    "ko": {"name": "한국어", "speech": "ko-KR"},
    "zh": {"name": "中文", "speech": "zh-CN"},
    "ru": {"name": "Русский", "speech": "ru-RU"},
    "tr": {"name": "Türkçe", "speech": "tr-TR"},
}

SCRIPT_LANGUAGES = (
    ("hi", r"[\u0900-\u097F]"),
    ("bn", r"[\u0980-\u09FF]"),
    ("pa", r"[\u0A00-\u0A7F]"),
    ("gu", r"[\u0A80-\u0AFF]"),
    ("or", r"[\u0B00-\u0B7F]"),
    ("ta", r"[\u0B80-\u0BFF]"),
    ("te", r"[\u0C00-\u0C7F]"),
    ("kn", r"[\u0C80-\u0CFF]"),
    ("ml", r"[\u0D00-\u0D7F]"),
    ("ur", r"[\u0600-\u06FF]"),
    ("ja", r"[\u3040-\u30FF]"),
    ("ko", r"[\uAC00-\uD7AF]"),
    ("zh", r"[\u4E00-\u9FFF]"),
    ("ru", r"[\u0400-\u04FF]"),
)


class FeatureProviderError(Exception):
    """A safe provider error suitable for conversion into an HTTP response."""


DEFAULT_GROQ_MODEL = "qwen/qwen3.8-27b"
DEFAULT_GROQ_STT_MODEL = "whisper-large-v3"
DEFAULT_GROQ_TTS_MODEL = "canopylabs/orpheus-v1-english"
DEFAULT_GROQ_TTS_VOICE = "autumn"
DEFAULT_AI_TIMEOUT = 55.0


def _max_provider_concurrency() -> int:
    try:
        configured = int(os.getenv("AI_MAX_CONCURRENCY", "4"))
    except (TypeError, ValueError):
        configured = 4
    return max(1, min(configured, 32))


_provider_slots = asyncio.Semaphore(_max_provider_concurrency())

_groq_client: groq.Groq | None = None
_groq_client_lock = threading.Lock()
_groq_client_key: str | None = None
_active_groq_model: str | None = None
GROQ_KEYS = KeyRotator("GROQ_API_KEYS", ("GROQ_API_KEY",))
GROQ_CHAT_FALLBACK_MODELS = (
    "qwen/qwen3.8-27b",
    "qwen/qwen3.6-27b",
    "groq/compound-mini",
)


def provider_status() -> dict[str, Any]:
    return {
        "gemini": GEMINI_KEYS.available,
        "gemini_key_count": GEMINI_KEYS.count,
        "groq": GROQ_KEYS.available,
        "groq_key_count": GROQ_KEYS.count,
        "groq_model": _active_groq_model or (os.getenv("GROQ_MODEL") or "").strip() or DEFAULT_GROQ_MODEL,
        "groq_stt_model": (os.getenv("GROQ_STT_MODEL") or "").strip() or DEFAULT_GROQ_STT_MODEL,
        "tts": "groq-orpheus" if GROQ_KEYS.available else "browser-speech",
    }


def _ai_timeout() -> float:
    """Return a safe SDK timeout even when an environment value is malformed."""
    try:
        value = float(os.getenv("AI_REQUEST_TIMEOUT", str(DEFAULT_AI_TIMEOUT)))
    except (TypeError, ValueError):
        value = DEFAULT_AI_TIMEOUT
    return max(5.0, min(value, 120.0))


async def _bounded_sync_call(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking SDK call off the event loop with a hard upper bound."""
    try:
        async with _provider_slots:
            return await asyncio.wait_for(
                asyncio.to_thread(function, *args, **kwargs),
                timeout=_ai_timeout(),
            )
    except asyncio.TimeoutError as exc:
        record_provider_event("provider_call", "failure", provider="groq", code="timeout")
        raise FeatureProviderError(
            "The coaching engine took too long to respond. Please try again."
        ) from exc


async def _preferred_chat(
    system: str,
    prompt: str,
    *,
    temperature: float = 0.35,
    max_tokens: int = 900,
    json_mode: bool = False,
) -> tuple[str, str]:
    """Try Groq first, then Gemini, and report which provider answered."""
    groq_error: FeatureProviderError | None = None
    if GROQ_KEYS.available:
        try:
            response = await _bounded_sync_call(
                _chat_sync,
                system,
                prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
            return str(response).strip(), "groq"
        except FeatureProviderError as exc:
            groq_error = exc

    if gemini_is_configured():
        gemini_prompt = prompt
        if json_mode:
            gemini_prompt += "\nReturn only the valid JSON object or array requested above."
        try:
            response, _ = await gemini_generate_text(
                system_instruction=system,
                prompt=gemini_prompt,
                temperature=temperature,
            )
            if response.strip():
                return response.strip(), "google-genai"
            raise FeatureProviderError("The conversational coach returned no answer.")
        except GeminiProviderError as exc:
            logger.warning("Gemini fallback request failed: %s", getattr(exc, "code", "provider_error"))

    if groq_error:
        raise groq_error
    raise FeatureProviderError("No conversational AI provider is available.")


def detect_language(text: str, preferred: str = "en") -> str:
    for language, pattern in SCRIPT_LANGUAGES:
        if re.search(pattern, text or ""):
            return language
    return preferred if preferred in LANGUAGES else "en"


def _fallback_coach(message: str, mode: str) -> str:
    focus = {
        "coach": "your preparation goal",
        "interview": "the role you are targeting",
        "debate": "your claim and the strongest clash",
        "confidence": "one calm, specific next step",
        "strategy": "a clear structure before you speak",
        "feedback": "one concrete example and its result",
    }.get(mode, "your preparation goal")
    return (
        f"Start with one clear point about {focus}. Then support it with a "
        "specific example, explain what you did, and close with the result. "
        f"For your note — “{message[:120]}” — write one sentence you can say "
        "out loud and one detail that proves it."
    )


def _fallback_debate(motion: str, ai_side: str) -> str:
    return (
        f"As {ai_side} on “{motion}”, our position is that the policy must be "
        "judged by its real-world impact, not only its intention. The key "
        "clash is who bears the risk and whether a workable safeguard exists. "
        "I invite you to answer that trade-off directly."
    )


def _fallback_gd(
    topic: str,
    names: list[str],
    history: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    prompts = [
        (
            "The immediate benefit is clear, but implementation and access need equal attention.",
            "I agree with the direction; could we anchor it in one concrete example and name who is affected?",
        ),
        (
            "Before we support that proposal, we should ask who might be excluded and what evidence would show the policy is working.",
            "That is a useful concern. A practical compromise needs a clear owner, a timeline, and a way to measure its impact.",
        ),
        (
            "The strongest part of this argument is its effect on everyday participants, but the long-term cost deserves a direct comparison.",
            "Can we connect both sides into one recommendation rather than treating access and feasibility as separate problems?",
        ),
        (
            "I see a distinction between the principle and how it would be implemented. The group should test both before reaching a conclusion.",
            "A workable next step would be to define the safeguard first, then decide whether the proposal still delivers enough benefit.",
        ),
        (
            "Let us separate the intended outcome from the mechanism. The proposal is only persuasive if the mechanism can survive real-world constraints.",
            "I would add an equity test: who gains first, who carries the cost, and what would make the result fairer?",
        ),
        (
            "The key uncertainty is not whether the goal matters but whether this is the best route to reach it.",
            "We could strengthen the case by comparing this option with one realistic alternative instead of treating it as all or nothing.",
        ),
        (
            "That point changes the trade-off: short-term convenience may create a longer-term dependency or cost.",
            "A useful synthesis would keep the benefit while adding a safeguard, a responsible owner, and a review date.",
        ),
        (
            "I hear a principle and a practical objection in the room. We should weigh them against the same standard.",
            "The missing piece is a testable example. What result would convince us that this recommendation is working?",
        ),
    ]
    prior = history or []
    turn = sum(1 for item in prior if item.get("speaker") in {"You", "user"})
    normalise = lambda value: re.sub(r"\W+", " ", value.lower()).strip()
    used = {
        normalise(str(item.get("content", "")))
        for item in prior
        if item.get("speaker") not in {"You", "user", "Moderator"}
    }
    ordered = prompts[turn % len(prompts):] + prompts[:turn % len(prompts)]
    first, second = next(
        (pair for pair in ordered if normalise(pair[0]) not in used and normalise(pair[1]) not in used),
        (
            f"On {compact_text(topic, 100)}, the next question is what evidence would change the group's view.",
            f"For this turn, I would turn that into a measurable recommendation with one benefit, one risk, and one safeguard.",
        ),
    )
    return [
        {"speaker": names[0] if names else "Maya", "content": first},
        {"speaker": names[1] if len(names) > 1 else "Arjun", "content": second},
    ]


def _groq_key() -> str:
    return GROQ_KEYS.current() or ""


def _json_value(text: str) -> Any:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def _groq() -> groq.Groq:
    """Create one official Groq SDK client per process."""
    global _groq_client, _groq_client_key
    key = _groq_key()
    if not key:
        raise FeatureProviderError("GROQ_API_KEY is not configured.")
    if _groq_client is None or _groq_client_key != key:
        with _groq_client_lock:
            if _groq_client is None or _groq_client_key != key:
                try:
                    _groq_client = groq.Groq(
                        api_key=key,
                        timeout=_ai_timeout(),
                        max_retries=1,
                    )
                    _groq_client_key = key
                except (groq.APIError, TypeError, ValueError) as exc:
                    logger.warning("Groq client could not be initialised: %s", type(exc).__name__)
                    raise FeatureProviderError("The conversational coach is unavailable right now.") from exc
    return _groq_client


def _reset_groq_client() -> None:
    """Forget the client bound to a failed key before trying the next key."""
    global _groq_client, _groq_client_key
    with _groq_client_lock:
        _groq_client = None
        _groq_client_key = None


def _groq_chat_models() -> list[str]:
    """Return the configured model followed by known current SDK fallbacks."""
    configured = (os.getenv("GROQ_MODEL") or "").strip() or DEFAULT_GROQ_MODEL
    candidates = [_active_groq_model, configured, *GROQ_CHAT_FALLBACK_MODELS]
    return list(dict.fromkeys(item for item in candidates if item))


def _model_not_available(exc: Exception) -> bool:
    """Identify model retirement/configuration errors without leaking provider details."""
    return getattr(exc, "status_code", None) == 404 or type(exc).__name__ in {
        "NotFoundError",
        "ConflictError",
    }


def _chat_sync(
    system: str,
    user: str,
    *,
    temperature: float = 0.35,
    max_tokens: int = 1200,
    json_mode: bool = False,
) -> str:
    global _active_groq_model
    last_error: Exception | None = None
    for key_attempt in range(max(1, GROQ_KEYS.count)):
        rotate_key = False
        for model in _groq_chat_models():
            try:
                request: dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if json_mode:
                    request["response_format"] = {"type": "json_object"}
                result = _groq().chat.completions.create(**request)
                choices = getattr(result, "choices", None) or []
                content = (
                    getattr(getattr(choices[0], "message", None), "content", "")
                    if choices
                    else ""
                ) or ""
                if content.strip():
                    _active_groq_model = model
                    return content.strip()
                last_error = FeatureProviderError("The conversational coach returned no answer.")
            except groq.APIError as exc:
                last_error = exc
                if _model_not_available(exc):
                    logger.warning("Configured Groq chat model is unavailable; trying a fallback.")
                    continue
                if GROQ_KEYS.is_rotation_error(exc):
                    rotate_key = True
                    break
                logger.warning("Groq chat request failed: %s", type(exc).__name__)
                raise FeatureProviderError("The conversational coach is unavailable right now.") from exc
            except (TypeError, ValueError) as exc:
                logger.warning("Groq chat request returned an invalid request or response: %s", type(exc).__name__)
                raise FeatureProviderError("The conversational coach is unavailable right now.") from exc
        if rotate_key and key_attempt < GROQ_KEYS.count - 1:
            GROQ_KEYS.rotate()
            _reset_groq_client()
            continue
        break
    if isinstance(last_error, FeatureProviderError):
        raise last_error
    raise FeatureProviderError("The conversational coach is unavailable right now.") from last_error


async def coach_reply(
    message: str,
    history: list[dict[str, str]] | None = None,
    *,
    mode: str = "coach",
    language: str = "en",
    user_goal: str = "",
) -> tuple[str, str, str]:
    detected = detect_language(message, language)
    language_name = LANGUAGES[detected]["name"]
    context = compact_history(history, max_turns=8, max_chars=4800)
    prompt = (
        f"Mode: {mode}\nPreferred response language: {language_name} ({detected})\n"
        f"Learner goal: {compact_text(user_goal, 500)}\nConversation:\n{context}\n\n"
        f"Learner's latest message: {compact_text(message, 2600)}\n"
        "Answer directly in the requested language. Be warm but rigorous. "
        "Use short paragraphs and finish with one actionable exercise."
    )
    if not _groq_key():
        record_provider_event("coach_reply", "fallback", provider="local")
        return _fallback_coach(message, mode), detected, "local-coach"
    try:
        reply = await _bounded_sync_call(
            _chat_sync,
            "You are PrepSage, a precise academic and placement-preparation coach.",
            prompt,
            temperature=0.45,
            max_tokens=900,
        )
        record_provider_event("coach_reply", "success", provider="groq")
        return reply, detected, "groq"
    except FeatureProviderError:
        # A provider outage should not turn a coaching note into a broken page.
        record_provider_event("coach_reply", "fallback", provider="groq")
        return _fallback_coach(message, mode), detected, "local-coach-fallback"


async def practice_plan(
    mode: str, question_count: int, config: dict[str, Any], document_text: str = ""
) -> tuple[str, list[dict[str, str]]]:
    """Generate the same compact plan shape as the Gemini rehearsal engine."""
    context = compact_mapping(config, 2400)
    prompt = (
        f"Create exactly {question_count} prompts for a {mode.replace('_', ' ')} rehearsal.\n"
        f"Context:\n{context}\nCandidate document:\n{compact_text(document_text, 2600, preserve_ends=True)}\n"
        "Return ONLY JSON: {title:string, questions:[{prompt:string, focus:string, guidance:string}]}."
        " Prompts must be distinct, self-contained, under 60 words, and written in the requested language."
    )
    raw = await _bounded_sync_call(
        _chat_sync,
        "You are a rigorous placement-preparation coach designing a practical rehearsal.",
        prompt,
        temperature=0.75,
        max_tokens=1500,
        json_mode=True,
    )
    result = _json_value(raw)
    if not isinstance(result, dict):
        raise FeatureProviderError("The coach returned an invalid rehearsal plan.")
    questions = [
        {
            "prompt": str(item.get("prompt", ""))[:1000],
            "focus": str(item.get("focus", ""))[:160],
            "guidance": str(item.get("guidance", ""))[:500],
        }
        for item in (result.get("questions") or [])
        if isinstance(item, dict) and str(item.get("prompt", "")).strip()
    ][:question_count]
    if len(questions) != question_count:
        raise FeatureProviderError("The coach could not create the complete rehearsal plan.")
    return str(result.get("title") or f"{mode.replace('_', ' ').title()} rehearsal")[:200], questions


async def context_guidance(
    mode: str,
    context: str,
    values: dict[str, Any],
    *,
    language: str = "en",
    variation: int = 0,
    previous_guidance: str = "",
) -> tuple[str, str]:
    """Create a short, tailored coaching note for the practice setup brief."""
    mode_name = mode.replace("_", " ")
    focus = (
        str(values.get("role_target") or values.get("topic") or values.get("focus_area") or "").strip()
        or "the learner's preparation"
    )
    context = compact_text(context, 1600, preserve_ends=True)
    fallback_notes = [
        f"For {focus}, turn this context into one specific example, one decision you made, and one result you can defend in the {mode_name} room.",
        f"This is useful context for {focus}. Start with the pressure point, explain your own contribution, and finish with evidence that shows what changed.",
        f"I’ll use this note to make the {mode_name} prompts more relevant: name the situation, the trade-off you faced, and the lesson you want to demonstrate.",
        f"Your next step for {focus}: reduce this description to one clear claim, one supporting detail, and one question the panel should ask you.",
    ]
    fallback = fallback_notes[variation % len(fallback_notes)]
    if not _groq_key():
        record_provider_event("context_guidance", "fallback", provider="local")
        return fallback, "local-context-guidance"
    prompt = (
        f"Practice mode: {mode_name}\nFocus: {compact_text(focus, 300)}\n"
        f"Learner context: {context}\nResponse language: {LANGUAGES.get(language, LANGUAGES['en'])['name']}\n"
        f"Variation marker: {variation}\nPrevious note to avoid repeating: {compact_text(previous_guidance, 700)}\n\n"
        "Write one concise, specific coaching note under 55 words. Explain how this context "
        "should shape the next rehearsal and mention one concrete detail the learner should "
        "include. Do not repeat generic boilerplate, describe the request itself, or reuse "
        "the previous note's wording or conclusion."
    )
    try:
        reply = await _bounded_sync_call(
            _chat_sync,
            "You are a practical preparation coach giving a tailored setup note.",
            prompt,
            temperature=0.7,
            max_tokens=180,
        )
        reply = str(reply).strip()[:700]
        record_provider_event("context_guidance", "success", provider="groq")
        return reply or fallback, "groq"
    except FeatureProviderError:
        record_provider_event("context_guidance", "fallback", provider="groq")
        return fallback, "local-context-guidance"


async def practice_evaluation(
    mode: str, question: str, answer: str, config: dict[str, Any]
) -> dict[str, Any]:
    rubric = {
        "interview": "Relevance, Structure, Evidence, Communication, Impact",
        "group_discussion": "Content depth, Clarity, Initiative, Collaboration, Summarising",
        "debate": "Matter, Manner, Method, Role fulfilment, Engagement with clash",
        "coaching": "Self-awareness, Specificity, Action orientation, Reflection quality",
    }.get(mode, "Relevance, Structure, Evidence, Communication, Impact")
    raw = await _bounded_sync_call(
        _chat_sync,
        "You are a strict but constructive preparation assessor. Return only valid JSON.",
        (
            f"Mode: {mode}\nRubric: {rubric}\nContext: {compact_mapping(config, 2200)}\n"
            f"Prompt: {compact_text(question, 1800)}\nAnswer: {compact_text(answer, 5200, preserve_ends=True)}\n"
            "Return JSON with score number 0-10, feedback string under 90 words, "
            "strengths array max 3, improvements array max 3, and rubric array of "
            "{criterion,score,max_score,comment} objects."
        ),
        temperature=0.25,
        max_tokens=1000,
        json_mode=True,
    )
    result = _json_value(raw)
    result["score"] = max(0, min(10, float(result.get("score", 5))))
    result["strengths"] = [str(x)[:300] for x in (result.get("strengths") or [])[:3]]
    result["improvements"] = [str(x)[:300] for x in (result.get("improvements") or [])[:3]]
    result["feedback"] = str(result.get("feedback", ""))[:1000]
    result["rubric"] = [
        {
            "criterion": str(item.get("criterion", ""))[:120],
            "score": max(0, min(10, float(item.get("score", 5)))),
            "max_score": 10,
            "comment": str(item.get("comment", ""))[:300],
        }
        for item in (result.get("rubric") or [])[:8]
    ]
    return result


async def interview_turn(
    *,
    interviewer: str,
    role_target: str,
    company: str,
    objective: str,
    question: str,
    answer: str,
    transcript: list[dict[str, Any]],
    follow_up_used: bool,
    remaining_questions: list[str],
    language: str = "en",
) -> dict[str, Any]:
    """Create the next interviewer turn for the voice interview room.

    The reference application delegates follow-up behavior to its voice agent.
    PrepSage keeps the same behavior explicit and provider-safe: a main question
    gets one focused follow-up, then the room advances to the next planned
    question. The returned shape is deliberately small for the browser room.
    """
    names = {"lisa": "Explorer Lisa", "bob": "Empathetic Bob"}
    interviewer_name = names.get(interviewer, "Explorer Lisa")
    language_name = LANGUAGES.get(language, LANGUAGES["en"])["name"]
    history = compact_history(
        [
            {"role": item.get("speaker", "interviewer"), "content": item.get("content", "")}
            for item in transcript
        ],
        max_turns=10,
        max_chars=6000,
    )
    if not follow_up_used:
        fallback_question = (
            "Could you give me one concrete example, explain what you personally did, "
            "and tell me what changed as a result?"
        )
        fallback_reply = (
            "Thank you. That gives me a useful starting point. "
            + fallback_question
        )
    else:
        fallback_question = remaining_questions[0] if remaining_questions else ""
        fallback_reply = (
            "Thank you for making that specific. "
            + (f"Let's move to the next question: {fallback_question}" if fallback_question else
               "That completes the interview. Thank you for your time.")
        )
    if not GROQ_KEYS.available and not gemini_is_configured():
        record_provider_event("interview_turn", "fallback", provider="local")
        return {
            "reply": fallback_reply,
            "question": fallback_question,
            "follow_up": not follow_up_used and bool(fallback_question),
            "language": language,
            "interviewer": interviewer_name,
            "provider": "local-interview-fallback",
        }

    prompt = (
        f"Interviewer: {interviewer_name}\n"
        f"Role: {compact_text(role_target, 220)}\n"
        f"Company: {compact_text(company, 220) or 'not specified'}\n"
        f"Objective: {compact_text(objective, 500)}\n"
        f"Response language: {language_name}\n"
        f"Current question: {compact_text(question, 900)}\n"
        f"Candidate answer: {compact_text(answer, 4200, preserve_ends=True)}\n"
        f"Follow-up already used for this question: {'yes' if follow_up_used else 'no'}\n"
        f"Remaining planned questions: {compact_text(' | '.join(remaining_questions), 1800)}\n"
        f"Recent transcript:\n{history}\n\n"
        "Return ONLY JSON with exactly these keys: "
        "reply (a brief natural acknowledgement, no headings), "
        "question (the next question, or empty string when finished), "
        "follow_up (boolean). "
        "When follow-up already used, set follow_up false and use the first remaining "
        "planned question. When it has not been used, ask one precise follow-up that "
        "deepens the candidate's answer and set follow_up true. Keep each question "
        "under 30 words, stay on the objective, and do not repeat the current question."
    )
    try:
        raw, used_provider = await _preferred_chat(
            (
                "You are a professional, warm interview researcher. "
                "You ask concise open-ended questions and uncover concrete evidence. "
                "Never mention being an AI or the JSON format."
            ),
            prompt,
            temperature=0.45,
            max_tokens=650,
            json_mode=True,
        )
        result = _json_value(raw)
        if not isinstance(result, dict):
            raise ValueError("interview turn was not an object")
        reply = str(result.get("reply", "")).strip()[:900] or fallback_reply
        next_question = str(result.get("question", "")).strip()[:700]
        wants_follow_up = bool(result.get("follow_up")) and not follow_up_used
        if wants_follow_up and not next_question:
            next_question = fallback_question
        if not wants_follow_up and remaining_questions:
            next_question = next_question or remaining_questions[0]
        record_provider_event("interview_turn", "success", provider=used_provider)
        return {
            "reply": reply,
            "question": next_question,
            "follow_up": wants_follow_up,
            "language": detect_language(reply, language),
            "interviewer": interviewer_name,
            "provider": used_provider,
        }
    except (FeatureProviderError, ValueError, TypeError, json.JSONDecodeError):
        record_provider_event("interview_turn", "fallback", provider="groq")
        return {
            "reply": fallback_reply,
            "question": fallback_question,
            "follow_up": not follow_up_used and bool(fallback_question),
            "language": language,
            "interviewer": interviewer_name,
            "provider": "local-interview-fallback",
        }


async def debate_turn(
    motion: str,
    ai_side: str,
    learner_speech: str,
    speeches: list[dict[str, Any]],
    *,
    language: str = "en",
 ) -> tuple[str, str, str]:
    history = compact_history(
        [{"role": item.get("speaker", "speaker"), "content": item.get("content", "")} for item in speeches],
        max_turns=8,
        max_chars=4500,
    )
    if not GROQ_KEYS.available and not gemini_is_configured():
        record_provider_event("debate_turn", "fallback", provider="local")
        return _fallback_debate(motion, ai_side), language, "local-debate-fallback"
    try:
        reply, used_provider = await _preferred_chat(
            (
                "You are a sharp but fair WUDC debate opponent. Respond with one "
                "compact rebuttal speech, identify the central clash, and end with "
                "a direct question. Do not use markdown headings."
            ),
            f"Motion: {motion}\nYour side: {ai_side}\nLanguage: {LANGUAGES.get(language, LANGUAGES['en'])['name']}\n"
             f"Previous speeches:\n{history}\nLearner's latest speech:\n{compact_text(learner_speech, 4500, preserve_ends=True)}",
            temperature=0.55,
            max_tokens=700,
        )
        record_provider_event("debate_turn", "success", provider=used_provider)
        return reply, detect_language(reply, language), used_provider
    except FeatureProviderError:
        record_provider_event("debate_turn", "fallback", provider="all")
        return _fallback_debate(motion, ai_side), language, "local-debate-fallback"


async def debate_evaluation(
    motion: str, speeches: list[dict[str, Any]], *, language: str = "en"
) -> dict[str, Any]:
    learner = [item for item in speeches if item.get("speaker") == "user"]
    if not GROQ_KEYS.available and not gemini_is_configured():
        record_provider_event("debate_evaluation", "fallback", provider="local")
        score = min(92, 52 + len(learner) * 7)
        return {
            "overall_score": score,
            "argument_quality": min(10, 5 + len(learner)),
            "evidence_use": min(10, 5 + len(learner)),
            "reasoning": min(10, 5 + len(learner)),
            "strategic_positioning": min(10, 5 + len(learner)),
            "detailed_feedback": "Your case has a workable spine. Make each claim more explicit, then weigh its impact against the best opposing response.",
            "strengths": ["You engaged with the motion.", "You stayed active in the clash."],
            "areas_for_improvement": ["Use one concrete example per major claim.", "Signpost the comparison that wins the debate."],
            "winner": "user" if score >= 70 else "opponent",
        }
    transcript = compact_history(
        [{"role": item.get("speaker", "speaker"), "content": item.get("content", "")} for item in speeches],
        max_turns=14,
        max_chars=9000,
    )
    prompt = (
        f"Motion: {motion}\nLanguage: {LANGUAGES.get(language, LANGUAGES['en'])['name']}\n"
        f"Transcript:\n{transcript}\n\nReturn ONLY valid JSON with integer values 1-10 for "
        "argument_quality, evidence_use, reasoning, strategic_positioning; overall_score "
        "integer 0-100; detailed_feedback string; strengths array max 3; areas_for_improvement "
        "array max 3; winner one of user, opponent, draw."
    )
    try:
        raw, used_provider = await _preferred_chat(
            "You are a WUDC adjudicator. Reward comparative analysis, clash engagement, and strategic prioritisation.",
            prompt,
            temperature=0.2,
            max_tokens=900,
            json_mode=True,
        )
        result = _json_value(raw)
        if not isinstance(result, dict):
            raise ValueError("Debate evaluator returned a non-object JSON value.")
        result["overall_score"] = max(0, min(100, int(result.get("overall_score", 60))))
        for key in ("argument_quality", "evidence_use", "reasoning", "strategic_positioning"):
            result[key] = max(1, min(10, int(result.get(key, 5))))
        result["detailed_feedback"] = str(result.get("detailed_feedback") or "Your debate report is ready.")[:2000]
        for key in ("strengths", "areas_for_improvement"):
            value = result.get(key)
            if isinstance(value, list):
                result[key] = [str(item)[:500] for item in value[:3]]
            elif value:
                result[key] = [str(value)[:500]]
            else:
                result[key] = []
        if result.get("winner") not in {"user", "opponent", "draw"}:
            result["winner"] = "draw"
        record_provider_event("debate_evaluation", "success", provider=used_provider)
        return result
    except (FeatureProviderError, ValueError, TypeError, json.JSONDecodeError):
        record_provider_event("debate_evaluation", "fallback", provider="all")
        return {
            "overall_score": 60,
            "argument_quality": 6,
            "evidence_use": 5,
            "reasoning": 6,
            "strategic_positioning": 5,
            "detailed_feedback": "Your debate was recorded, but the adjudicator could not return a structured report. Try the next round with clearer signposting.",
            "strengths": ["You completed the round."],
            "areas_for_improvement": ["Connect each claim to a comparative impact."],
            "winner": "draw",
        }


async def gd_responses(
    topic: str,
    message: str,
    participants: list[str],
    history: list[dict[str, Any]],
    *,
    language: str = "en",
 ) -> tuple[list[dict[str, str]], str, str]:
    names = participants[:4] or ["Maya", "Arjun"]
    if not GROQ_KEYS.available and not gemini_is_configured():
        record_provider_event("gd_responses", "fallback", provider="local")
        return _fallback_gd(topic, names, history), language, "local-discussion-fallback"
    transcript = compact_history(
        [{"role": item.get("speaker", "speaker"), "content": item.get("content", "")} for item in history],
        max_turns=8,
        max_chars=4500,
    )
    prompt = (
        f"Topic: {topic}\nParticipants who may speak: {', '.join(names)}\n"
        f"Response language: {LANGUAGES.get(language, LANGUAGES['en'])['name']}\n"
        f"Recent discussion:\n{transcript}\nLearner's contribution:\n{compact_text(message, 2600, preserve_ends=True)}\n\n"
        "Return ONLY a JSON array with exactly two objects, each having speaker and content. "
        "Give the participants distinct viewpoints: one should test evidence or assumptions, "
        "the other should build toward synthesis. Keep each under 55 words. "
        "Do not reuse wording or conclusions from earlier participant messages; make this "
        "turn specific to the learner's latest contribution."
    )
    try:
        raw, used_provider = await _preferred_chat(
            "You moderate a realistic group discussion with concise, distinct participant voices.",
            prompt,
            temperature=0.65,
            max_tokens=500,
            json_mode=True,
        )
        result = _json_value(raw)
        if not isinstance(result, list):
            raise ValueError("not a list")
        responses = [
            {"speaker": str(item.get("speaker", names[idx % len(names)]))[:60], "content": str(item.get("content", ""))[:800]}
            for idx, item in enumerate(result[:2])
            if isinstance(item, dict) and str(item.get("content", "")).strip()
        ]
        if len(responses) != 2:
            raise ValueError("incomplete discussion response")
        normalise = lambda value: re.sub(r"\W+", " ", value.lower()).strip()
        prior_content = {
            normalise(str(item.get("content", "")))
            for item in history
            if item.get("speaker") not in {"You", "user", "Moderator"}
        }
        current_content = [normalise(item["content"]) for item in responses]
        if len(set(current_content)) != len(current_content) or any(
            content in prior_content for content in current_content
        ):
            raise ValueError("repetitive discussion response")
        record_provider_event("gd_responses", "success", provider=used_provider)
        return responses, language, used_provider
    except (FeatureProviderError, ValueError, TypeError, json.JSONDecodeError):
        record_provider_event("gd_responses", "fallback", provider="all")
        return _fallback_gd(topic, names, history), language, "local-discussion-fallback"


async def gd_evaluation(topic: str, messages: list[dict[str, Any]], *, language: str = "en") -> dict[str, Any]:
    learner = [item for item in messages if item.get("speaker") == "You"]
    if not GROQ_KEYS.available and not gemini_is_configured():
        record_provider_event("gd_evaluation", "fallback", provider="local")
        overall = min(10, 5 + len(learner))
        return {
            "overall_score": overall,
            "participation_score": overall,
            "content_score": max(1, overall - 1),
            "communication_score": overall,
            "leadership_score": max(1, overall - 2),
            "summary": "You contributed consistently. Build on this by adding evidence and bringing quieter viewpoints into the synthesis.",
            "strengths": ["You entered the discussion with a clear contribution."],
            "improvements": ["Use a concrete example.", "Summarise the group before closing."],
        }
    transcript = compact_history(
        [{"role": x.get("speaker", "speaker"), "content": x.get("content", "")} for x in messages],
        max_turns=16,
        max_chars=8500,
    )
    try:
        raw, used_provider = await _preferred_chat(
            "You are a group-discussion assessor. Return only valid JSON.",
             f"Topic: {compact_text(topic, 500)}\nLanguage: {LANGUAGES.get(language, LANGUAGES['en'])['name']}\nTranscript:\n{transcript}\n"
            "Return integer 1-10 fields overall_score, participation_score, content_score, "
            "communication_score, leadership_score; summary; strengths array max 3; improvements array max 3.",
            temperature=0.2,
            max_tokens=700,
            json_mode=True,
        )
        result = _json_value(raw)
        for key in ("overall_score", "participation_score", "content_score", "communication_score", "leadership_score"):
            result[key] = max(1, min(10, int(result.get(key, 5))))
        record_provider_event("gd_evaluation", "success", provider=used_provider)
        return result
    except (FeatureProviderError, ValueError, TypeError, json.JSONDecodeError):
        record_provider_event("gd_evaluation", "fallback", provider="all")
        return {
            "overall_score": 5,
            "participation_score": 5,
            "content_score": 5,
            "communication_score": 5,
            "leadership_score": 4,
            "summary": "The discussion was recorded. Try another round with a sharper example and a closing synthesis.",
            "strengths": ["You completed the discussion."],
            "improvements": ["Add evidence and summarise the emerging consensus."],
        }


async def practice_summary(
    mode: str,
    config: dict[str, Any],
    transcript: str,
    average_score: float,
) -> dict[str, Any]:
    """Summarise a completed rehearsal with the configured Groq chat model."""
    if not _groq_key():
        record_provider_event("practice_summary", "fallback", provider="local")
        raise FeatureProviderError("GROQ_API_KEY is not configured.")
    raw = await _bounded_sync_call(
        _chat_sync,
        "You are a precise preparation coach. Return only valid JSON.",
        (
            f"Mode: {mode}\nContext: {compact_mapping(config, 2200)}\n"
            f"Average answer score: {average_score:.1f}/10\n"
            f"Transcript:\n{compact_transcript(transcript, 8500)}\n"
            "Return JSON with readiness number 0-100, summary string under 120 words, "
            "strengths array max 3, improvements array max 3, and rubric array of "
            "{criterion,score,max_score,comment} objects. Score rubric items 0-10."
        ),
        temperature=0.25,
        max_tokens=900,
        json_mode=True,
    )
    result = _json_value(raw)
    result["readiness"] = max(0, min(100, float(result.get("readiness", average_score * 10))))
    result["summary"] = str(result.get("summary", ""))[:1600]
    result["strengths"] = [str(x)[:300] for x in (result.get("strengths") or [])[:3]]
    result["improvements"] = [str(x)[:300] for x in (result.get("improvements") or [])[:3]]
    result["rubric"] = [
        {
            "criterion": str(item.get("criterion", ""))[:120],
            "score": max(0, min(10, float(item.get("score", 5)))),
            "max_score": 10,
            "comment": str(item.get("comment", ""))[:300],
        }
        for item in (result.get("rubric") or [])[:8]
        if isinstance(item, dict)
    ]
    record_provider_event("practice_summary", "success", provider="groq")
    return result


async def transcribe_audio(data: bytes, filename: str, content_type: str, language: str = "en") -> dict[str, Any]:
    if not _groq_key():
        raise FeatureProviderError("Voice transcription needs a GROQ_API_KEY.")

    def send() -> Any:
        attempts = max(1, GROQ_KEYS.count)
        for attempt in range(attempts):
            try:
                return _groq().audio.transcriptions.create(
                    model=provider_status()["groq_stt_model"],
                    file=(filename or "answer.webm", data, content_type or "audio/webm"),
                    language=language if language in LANGUAGES else "en",
                    response_format="json",
                )
            except groq.APIError as exc:
                if attempt < attempts - 1 and GROQ_KEYS.rotate_after(exc):
                    logger.warning("Groq transcription request failed; trying the next key.")
                    _reset_groq_client()
                    continue
                logger.warning("Groq transcription request failed: %s", type(exc).__name__)
                raise FeatureProviderError(
                    "We could not transcribe that recording. You can type your answer instead."
                ) from exc
            except (TypeError, ValueError) as exc:
                logger.warning("Groq transcription request was invalid: %s", type(exc).__name__)
                raise FeatureProviderError(
                    "We could not transcribe that recording. You can type your answer instead."
                ) from exc

    result = await asyncio.to_thread(send)
    text = str(getattr(result, "text", "") or "").strip()
    return {"success": bool(text), "transcript": text, "language": detect_language(text, language), "service": "groq-whisper"}