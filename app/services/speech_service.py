"""Server-side speech helpers with a safe browser fallback."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import wave
from io import BytesIO
from typing import Any

import groq

from app.services.key_rotator import KeyRotator

logger = logging.getLogger(__name__)
TTS_KEYS = KeyRotator("GROQ_API_KEYS", ("GROQ_API_KEY",))

TTS_VOICES = {
    "lisa": "autumn",
    "bob": "troy",
}
ARABIC_TTS_VOICES = {"lisa": "noura", "bob": "fahad"}
ORPHEUS_ENGLISH_MODEL = "canopylabs/orpheus-v1-english"
ORPHEUS_ARABIC_MODEL = "canopylabs/orpheus-arabic-saudi"
SUPPORTED_GROQ_TTS_MODELS = {ORPHEUS_ENGLISH_MODEL, ORPHEUS_ARABIC_MODEL}


def _fallback(text: str, language: str, reason: str = "") -> dict[str, Any]:
    return {
        "success": False,
        "text": text,
        "language": language,
        "service": "browser",
        "browser_fallback": True,
        "error": reason or "Browser speech playback will be used.",
    }


def _split_tts_text(text: str, limit: int = 190) -> list[str]:
    """Split text at word boundaries to stay below Orpheus's 200-char cap."""
    chunks: list[str] = []
    current = ""
    for word in re.findall(r"\S+", text.strip()):
        while len(word) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(word[:limit])
            word = word[limit:]
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > limit:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _join_wav_audio(parts: list[bytes]) -> bytes:
    """Join same-format PCM WAV parts into one continuous audio file."""
    if len(parts) == 1:
        return parts[0]
    frames: list[bytes] = []
    params = None
    for part in parts:
        with wave.open(BytesIO(part), "rb") as source:
            source_params = source.getparams()
            if source_params.comptype != "NONE":
                raise ValueError("Compressed WAV audio cannot be joined.")
            if params is None:
                params = source_params
            elif (
                source_params[:3] != params[:3]
                or source_params.comptype != params.comptype
            ):
                raise ValueError("TTS segments returned incompatible WAV formats.")
            frames.append(source.readframes(source.getnframes()))
    output = BytesIO()
    with wave.open(output, "wb") as destination:
        destination.setnchannels(params.nchannels)
        destination.setsampwidth(params.sampwidth)
        destination.setframerate(params.framerate)
        destination.setcomptype(params.comptype, params.compname)
        destination.writeframes(b"".join(frames))
    return output.getvalue()


def _synthesize_sync(text: str, language: str, voice_profile: str) -> dict[str, Any]:
    groq_result = _synthesize_groq(text, language, voice_profile)
    if groq_result:
        return groq_result
    return _fallback(text, language, "Server audio is unavailable; using the selected browser voice.")


def _synthesize_groq(
    text: str, language: str, voice_profile: str
) -> dict[str, Any] | None:
    """Use Orpheus TTS for supported languages; otherwise use browser speech."""
    if not TTS_KEYS.available:
        return None
    normalized_language = (language or "en").lower()
    if normalized_language == "ar":
        model = ORPHEUS_ARABIC_MODEL
        voice = ARABIC_TTS_VOICES[voice_profile]
    elif normalized_language == "en":
        configured_model = (os.getenv("GROQ_TTS_MODEL") or "").strip()
        model = (
            configured_model
            if configured_model in SUPPORTED_GROQ_TTS_MODELS
            else ORPHEUS_ENGLISH_MODEL
        )
        voice = TTS_VOICES[voice_profile]
    else:
        return None
    try:
        timeout = max(5.0, min(float(os.getenv("AI_REQUEST_TIMEOUT", "55")), 120.0))
    except (TypeError, ValueError):
        timeout = 55.0
    for _ in range(max(1, TTS_KEYS.count)):
        key = TTS_KEYS.current()
        if not key:
            return None
        try:
            client = groq.Groq(
                api_key=key,
                timeout=timeout,
                max_retries=1,
            )
            parts: list[bytes] = []
            for chunk in _split_tts_text(text[:5000]):
                response = client.audio.speech.create(
                    input=chunk,
                    model=model,
                    voice=voice,
                    response_format="wav",
                )
                data = (
                    response.read()
                    if callable(getattr(response, "read", None))
                    else getattr(response, "content", b"")
                )
                if not data:
                    raise ValueError("TTS returned an empty audio segment.")
                parts.append(data)
            data = _join_wav_audio(parts)
            if data:
                return {
                    "success": True,
                    "text": text,
                    "audio_data": base64.b64encode(data).decode("ascii"),
                    "mime_type": "audio/wav",
                    "service": "groq-orpheus",
                    "voice": voice,
                    "voice_profile": voice_profile,
                    "browser_fallback": True,
                }
            return None
        except Exception as exc:  # noqa: BLE001 - provider details stay server-side
            logger.warning(
                "Groq TTS request failed: %s status=%s",
                type(exc).__name__,
                getattr(exc, "status_code", "n/a"),
            )
            if not TTS_KEYS.rotate_after(exc):
                return None
    return None


async def synthesize_text(
    text: str, language: str = "en", voice_profile: str = "lisa"
) -> dict[str, Any]:
    """Return WAV audio when possible, otherwise a safe browser fallback."""
    profile = voice_profile if voice_profile in TTS_VOICES else "lisa"
    return await asyncio.to_thread(_synthesize_sync, text, language, profile)