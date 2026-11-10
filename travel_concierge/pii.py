"""PII redaction utilities for prompts, tool logs, traces, and persistent memory.

Scrubs sensitive personal data (credit cards, US SSNs, passport identifiers,
email addresses, phone numbers, and bearer/API tokens) before text reaches
LLM calls, structured logs, OpenTelemetry span attributes, or long-term memory.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

# Order matters: scrub high-specificity tokens/cards/SSNs before generic phones.
_BEARER_OR_API_KEY_RE = re.compile(
    r"(?:Bearer\s+[A-Za-z0-9\-._~+/]+=*|AIza[0-9A-Za-z\-_]{35})"
)
_SSN_RE = re.compile(
    r"\b(?!000|666|9\d\d)\d{3}[-\s](?!00)\d{2}[-\s](?!0000)\d{4}\b"
)
_CREDIT_CARD_CANDIDATE_RE = re.compile(
    r"\b(?:\d[ -]*?){13,19}\b"
)
_PASSPORT_RE = re.compile(
    r"(?i)\b(passport(?:\s*(?:no\.?|number|id|#))?\s*[:=#-]?\s*)([A-Z0-9]{6,9})\b"
)
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
# Matches US and common international phone formats while avoiding ISO dates (2026-11-09).
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\b\d{3})[\s.-]\d{3}[\s.-]\d{4}\b"
)


def _passes_luhn(digits: str) -> bool:
    """Validate a digit string with the Luhn checksum algorithm."""
    if not (13 <= len(digits) <= 19) or not digits.isdigit():
        return False
    if len(set(digits)) == 1:
        return False
    checksum = 0
    reverse_digits = digits[::-1]
    for idx, ch in enumerate(reverse_digits):
        val = int(ch)
        if idx % 2 == 1:
            val *= 2
            if val > 9:
                val -= 9
        checksum += val
    return checksum % 10 == 0


def _looks_like_card_prefix(digits: str) -> bool:
    """Check common payment card test/issuer prefixes alongside 15-16 digit structure."""
    if len(digits) not in (15, 16):
        return False
    return digits.startswith(("4", "51", "52", "53", "54", "55", "34", "37", "6011"))


def redact_pii_text(text: str) -> tuple[str, list[str]]:
    """Redact PII tokens from a string and report detected PII categories.

    Args:
        text: Untrusted text from a user message, tool payload, log field, or
            model output.

    Returns:
        A tuple of `(scrubbed_text, detected_categories)` where
        `detected_categories` is a deduplicated ordered list such as
        `["CREDIT_CARD", "EMAIL"]`.
    """
    if not text:
        return text, []

    detected: list[str] = []

    def _record(category: str) -> None:
        if category not in detected:
            detected.append(category)

    scrubbed = text

    if _BEARER_OR_API_KEY_RE.search(scrubbed):
        scrubbed = _BEARER_OR_API_KEY_RE.sub("[REDACTED_SECRET]", scrubbed)
        _record("SECRET_TOKEN")

    if _SSN_RE.search(scrubbed):
        scrubbed = _SSN_RE.sub("[REDACTED_SSN]", scrubbed)
        _record("SSN")

    def _replace_card(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        if _passes_luhn(digits) or (
            ("-" in raw or " " in raw) and _looks_like_card_prefix(digits)
        ):
            _record("CREDIT_CARD")
            return "[REDACTED_CREDIT_CARD]"
        return raw

    scrubbed = _CREDIT_CARD_CANDIDATE_RE.sub(_replace_card, scrubbed)

    def _replace_passport(match: re.Match[str]) -> str:
        candidate = match.group(2)
        # Require at least one digit so ordinary English words after "passport" survive.
        if any(ch.isdigit() for ch in candidate):
            _record("PASSPORT")
            return f"{match.group(1)}[REDACTED_PASSPORT]"
        return match.group(0)

    scrubbed = _PASSPORT_RE.sub(_replace_passport, scrubbed)

    if _EMAIL_RE.search(scrubbed):
        scrubbed = _EMAIL_RE.sub("[REDACTED_EMAIL]", scrubbed)
        _record("EMAIL")

    if _PHONE_RE.search(scrubbed):
        scrubbed = _PHONE_RE.sub("[REDACTED_PHONE]", scrubbed)
        _record("PHONE")

    return scrubbed, detected


def redact_pii(value: Any) -> Any:
    """Recursively redact PII from strings, mappings, lists, and tuples.

    Args:
        value: Arbitrary JSON-like payload or Python object.

    Returns:
        A deep-copied structure with all string leaves scrubbed of PII and
        sensitive dictionary keys (`password`, `api_key`, `secret`, `token`,
        `ssn`, `credit_card`, `passport_number`) masked unconditionally.
    """
    sensitive_keys = {
        "password",
        "api_key",
        "apikey",
        "secret",
        "access_token",
        "refresh_token",
        "authorization",
        "ssn",
        "credit_card",
        "card_number",
        "cvv",
        "passport_number",
    }

    if isinstance(value, str):
        scrubbed, _ = redact_pii_text(value)
        return scrubbed
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if key_str.lower() in sensitive_keys and v is not None:
                sanitized[key_str] = "[REDACTED_SENSITIVE_FIELD]"
            else:
                sanitized[key_str] = redact_pii(v)
        return sanitized
    if isinstance(value, list):
        return [redact_pii(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_pii(item) for item in value)
    return value


def mask_identifier_for_display(identifier: str, visible_suffix: int = 4) -> str:
    """Mask an identifier for safe user-facing display (e.g., `***-***-1234`)."""
    cleaned = identifier.strip()
    if len(cleaned) <= visible_suffix:
        return "*" * len(cleaned)
    return f"***-***-{cleaned[-visible_suffix:]}"
