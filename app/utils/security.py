"""Standard-library security primitives for PrepSage AI.

No secrets are ever logged or returned from this module.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 390_000
SALT_BYTES = 16
TOKEN_BYTES = 32

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128

COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "qwerty123",
        "letmein123",
        "welcome123",
        "admin12345",
        "iloveyou123",
        "1234567890",
        "prepsage123",
    }
)


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #


def normalize_email(raw: str) -> str:
    """Trim, NFKC-normalize and lowercase an email address."""
    value = unicodedata.normalize("NFKC", (raw or "")).strip()
    if "@" not in value:
        return value.lower()
    local, _, domain = value.rpartition("@")
    return f"{local.lower()}@{domain.lower()}"


def validate_email(raw: str) -> str | None:
    """Return an error message, or None when the email looks valid."""
    email = normalize_email(raw)
    if not email:
        return "Email address is required."
    if len(email) > 320:
        return "Email address is too long."
    if ".." in email or email.startswith(".") or "@." in email:
        return "Enter a valid email address."
    if not EMAIL_RE.match(email):
        return "Enter a valid email address, e.g. you@school.edu."
    return None


# --------------------------------------------------------------------------- #
# Password rules
# --------------------------------------------------------------------------- #


def validate_password(
    password: str, email: str = "", full_name: str = ""
) -> str | None:
    """Return an error message, or None when the password is strong enough."""
    if not password:
        return "Password is required."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Use at most {MAX_PASSWORD_LENGTH} characters."
    if password.strip() != password:
        return "Password cannot start or end with a space."
    if not any(c.islower() for c in password):
        return "Include at least one lowercase letter."
    if not any(c.isupper() for c in password):
        return "Include at least one uppercase letter."
    if not any(c.isdigit() for c in password):
        return "Include at least one number."
    if not any(not c.isalnum() for c in password):
        return "Include at least one symbol, such as ! ? # or -."
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS:
        return "That password is too common. Choose something more distinctive."
    local = normalize_email(email).split("@")[0]
    if local and len(local) >= 4 and local in lowered:
        return "Password cannot contain your email name."
    for part in (full_name or "").split():
        if len(part) >= 4 and part.lower() in lowered:
            return "Password cannot contain your name."
    if len(set(lowered)) < 5:
        return "Password is too repetitive."
    return None


def password_strength(password: str) -> tuple[int, str]:
    """Coarse 0-4 strength score with a label. Never logged or stored."""
    if not password:
        return 0, "Empty"
    score = 0
    score += 1 if len(password) >= MIN_PASSWORD_LENGTH else 0
    score += 1 if len(password) >= 14 else 0
    score += (
        1
        if any(c.isupper() for c in password)
        and any(c.islower() for c in password)
        else 0
    )
    score += (
        1
        if any(c.isdigit() for c in password)
        and any(not c.isalnum() for c in password)
        else 0
    )
    labels = ["Very weak", "Weak", "Fair", "Strong", "Excellent"]
    return score, labels[min(score, 4)]


# --------------------------------------------------------------------------- #
# PBKDF2-HMAC hashing
# --------------------------------------------------------------------------- #


def generate_salt() -> str:
    return secrets.token_hex(SALT_BYTES)


def hash_password(
    password: str, salt: str, iterations: int = PBKDF2_ITERATIONS
) -> str:
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        iterations,
    )
    return derived.hex()


def verify_password(
    password: str, salt: str, expected_hash: str, iterations: int
) -> bool:
    """Constant-time password verification."""
    if not salt or not expected_hash:
        return False
    try:
        candidate = hash_password(password, salt, iterations)
    except ValueError:
        return False
    return hmac.compare_digest(candidate, expected_hash)


# --------------------------------------------------------------------------- #
# Opaque session tokens
# --------------------------------------------------------------------------- #


def generate_session_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    return (token or "")[:10]
