"""Guardrails and Policy Plugin enforcing PII privacy, injection blocks, and spend caps.

Enforces defense-in-depth at four interception boundaries:
1. `on_user_message_callback`: Scrubs PII before session persistence.
2. `before_model_callback`: Screens for prompt injection / jailbreak attempts
   (fail-closed short-circuit) and scrubs PII across `llm_request.contents`.
3. `before_tool_callback`: Blocks Level-4 ("Do Not Travel") bookings, enforces
   single-booking spend ceilings, and sanitizes tool arguments.
4. `after_model_callback`: Scrubs any residual PII from generated text.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from travel_concierge.config import load_settings
from travel_concierge.database import normalize_location_code
from travel_concierge.observability import log_structured_event
from travel_concierge.pii import redact_pii, redact_pii_text

# Prompt-injection & policy-bypass patterns screened pre-LLM (fail-closed).
_PROMPT_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"(?i)\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:previous|prior|system|above)\s+(?:instructions|rules|prompts|constraints)\b"
        ),
    ),
    (
        "system_prompt_exfiltration",
        re.compile(
            r"(?i)\b(?:reveal|print|dump|output|show)\s+(?:your\s+)?(?:exact\s+|full\s+)?(?:system\s+prompt|hidden\s+instructions|developer\s+message|api\s+key)\b"
        ),
    ),
    (
        "guardrail_bypass",
        re.compile(
            r"(?i)\b(?:bypass|disable|skip|override)\s+(?:human\s+confirmation|safety\s+guardrails?|hitl|approval\s+gate|spending\s+limit)\b"
        ),
    ),
    (
        "sql_or_shell_payload",
        re.compile(
            r"(?i)(?:;\s*DROP\s+TABLE\b|UNION\s+SELECT\b|\$\(\s*(?:rm|curl|wget|bash|sh)\b)"
        ),
    ),
)

# Level-4 Do-Not-Travel destination codes blocked from reservation tools.
LEVEL_4_BLOCKED_DESTINATIONS: frozenset[str] = frozenset({"KBL"})


def detect_prompt_injection(text: str) -> str | None:
    """Return the matched policy rule ID if untrusted text contains an injection attempt."""
    if not text:
        return None
    for rule_name, pattern in _PROMPT_INJECTION_PATTERNS:
        if pattern.search(text):
            return rule_name
    return None


def scrub_genai_content_in_place(content: types.Content | None) -> list[str]:
    """Scrub PII from all text parts of a `google.genai.types.Content` object."""
    detected_all: list[str] = []
    if not content or not getattr(content, "parts", None):
        return detected_all

    for part in content.parts:
        raw_text = getattr(part, "text", None)
        if isinstance(raw_text, str) and raw_text:
            scrubbed, categories = redact_pii_text(raw_text)
            if categories:
                part.text = scrubbed
                for cat in categories:
                    if cat not in detected_all:
                        detected_all.append(cat)
    return detected_all


def screen_and_sanitize_llm_request(
    agent_name: str,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
    """Inspect and scrub an `LlmRequest` prior to calling Gemini.

    Returns `None` when safe to continue, or a safe `LlmResponse` refusal to
    short-circuit the model call when a policy violation is detected.
    """
    contents = getattr(llm_request, "contents", None) or []

    # Screen the latest user message first for prompt injection / policy bypass.
    for content in reversed(contents):
        if getattr(content, "role", "") == "user":
            for part in getattr(content, "parts", None) or []:
                raw_text = getattr(part, "text", None)
                if isinstance(raw_text, str):
                    rule = detect_prompt_injection(raw_text)
                    if rule:
                        log_structured_event(
                            "guardrail.prompt_blocked",
                            "Blocked prompt-injection or policy-bypass attempt before LLM call",
                            severity=logging.WARNING,
                            agent_name=agent_name,
                            policy_rule=rule,
                        )
                        return LlmResponse(
                            content=types.Content(
                                role="model",
                                parts=[
                                    types.Part.from_text(
                                        text=(
                                            "I cannot fulfill requests to ignore safety rules, "
                                            "reveal internal instructions, bypass booking "
                                            "confirmations, or process unsafe payloads. "
                                            "I would be happy to help plan a safe, "
                                            "budget-friendly trip instead."
                                        )
                                    )
                                ],
                            )
                        )
            break

    # Scrub PII across all content turns before transmitting to Gemini.
    redacted_categories: list[str] = []
    for content in contents:
        cats = scrub_genai_content_in_place(content)
        for cat in cats:
            if cat not in redacted_categories:
                redacted_categories.append(cat)

    if redacted_categories:
        log_structured_event(
            "guardrail.pii_redacted_pre_llm",
            "Redacted sensitive PII from prompt prior to Gemini invocation",
            agent_name=agent_name,
            pii_categories=redacted_categories,
        )

    return None


def validate_tool_policy(
    tool_name: str,
    tool_args: dict[str, Any],
    agent_name: str = "",
) -> Optional[dict[str, Any]]:
    """Enforce deterministic pre-execution tool guardrails and scrub PII in args.

    Mutates string values in `tool_args` to mask PII before tool execution and
    returns an actionable error payload to short-circuit execution if a travel
    safety or spend-cap rule is violated.
    """
    settings = load_settings()

    # Scrub PII in-place for string fields such as `special_requests` or `preference_value`.
    for key, val in list(tool_args.items()):
        if isinstance(val, str):
            scrubbed, cats = redact_pii_text(val)
            if cats:
                tool_args[key] = scrubbed
                log_structured_event(
                    "guardrail.pii_redacted_tool_arg",
                    f"Scrubbed PII from argument '{key}' of tool '{tool_name}'",
                    agent_name=agent_name,
                    tool_name=tool_name,
                    pii_categories=cats,
                )
        elif isinstance(val, (dict, list)):
            tool_args[key] = redact_pii(val)

    # Block flight/hotel searches or reservations targeting Level-4 Do-Not-Travel zones.
    if tool_name in {"search_scheduled_flights", "search_vetted_hotels"}:
        raw_dest = str(tool_args.get("destination", ""))
        code = normalize_location_code(raw_dest)
        if code in LEVEL_4_BLOCKED_DESTINATIONS:
            log_structured_event(
                "guardrail.level4_destination_blocked",
                f"Blocked {tool_name} for Level-4 destination {code}",
                severity=logging.WARNING,
                agent_name=agent_name,
                tool_name=tool_name,
                destination_code=code,
            )
            return {
                "status": "error",
                "error_code": "SAFETY_LEVEL_4_DO_NOT_TRAVEL",
                "message": (
                    f"Destination '{code}' carries a Level 4 ('Do Not Travel') "
                    "safety advisory. TripPilot policy prohibits quoting or "
                    "booking flights and hotels for Level-4 destinations."
                ),
                "recovery_guidance": (
                    "Inform the traveler that Level-4 destinations cannot be booked "
                    "for safety reasons, call `retrieve_destination_advisory` only "
                    "if advisory details are needed, and offer safe Level 1-2 "
                    "alternatives such as LIS, TYO, PAR, MEX, or LON."
                ),
            }

    if tool_name == "reserve_approved_itinerary":
        flight_id = str(tool_args.get("flight_id", "")).upper()
        hotel_id = str(tool_args.get("hotel_id", "")).upper()
        for blocked in LEVEL_4_BLOCKED_DESTINATIONS:
            if f"-{blocked}-" in flight_id or f"-{blocked}-" in hotel_id:
                return {
                    "status": "error",
                    "error_code": "SAFETY_LEVEL_4_DO_NOT_TRAVEL",
                    "message": "Reservations for Level-4 destinations are blocked by safety policy.",
                    "recovery_guidance": "Select a vetted Level 1-2 destination before booking.",
                }

        approved_budget = tool_args.get("approved_budget_usd")
        if isinstance(approved_budget, (int, float)) and approved_budget > settings.max_single_booking_usd:
            log_structured_event(
                "guardrail.spend_cap_blocked",
                "Blocked reservation exceeding max single-booking spend ceiling",
                severity=logging.WARNING,
                agent_name=agent_name,
                approved_budget_usd=approved_budget,
                max_single_booking_usd=settings.max_single_booking_usd,
            )
            return {
                "status": "error",
                "error_code": "SPEND_CAP_EXCEEDED",
                "message": (
                    f"Requested booking budget (${approved_budget:,.2f}) exceeds the "
                    f"policy ceiling of ${settings.max_single_booking_usd:,.2f}."
                ),
                "recovery_guidance": (
                    "Select lower-cost economy/boutique catalog options or reduce trip "
                    f"length so total spend stays at or below ${settings.max_single_booking_usd:,.2f}."
                ),
            }

    return None


class TravelSafetyPolicyPlugin(BasePlugin):
    """ADK Plugin applying PII scrubbing, injection defense, and travel policy checks."""

    def __init__(self, name: str = "trippilot_safety_policy") -> None:
        super().__init__(name=name)

    async def on_user_message_callback(
        self,
        *,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> Optional[types.Content]:
        """Scrub PII on ingress before the user message is persisted to session events.

        Running at ingress keeps SSNs, card numbers, passport IDs, emails, and phone
        numbers out of the SQLite/Cloud SQL session tables, not just out of prompts.
        Prompt-injection screening still happens in `before_model_callback`, because
        redaction never removes the instruction-override phrases it looks for.
        """
        cats = scrub_genai_content_in_place(user_message)
        if cats:
            session = getattr(invocation_context, "session", None)
            log_structured_event(
                "guardrail.pii_redacted_ingress",
                "Scrubbed PII from incoming user message before session persistence",
                session_id=str(getattr(session, "id", "")),
                pii_categories=cats,
            )
        return user_message

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        from travel_concierge.memory import compact_llm_request_history

        compact_llm_request_history(llm_request, agent_name=callback_context.agent_name)
        return screen_and_sanitize_llm_request(callback_context.agent_name, llm_request)

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> Optional[LlmResponse]:
        content = getattr(llm_response, "content", None)
        cats = scrub_genai_content_in_place(content)
        if cats:
            log_structured_event(
                "guardrail.pii_redacted_post_llm",
                "Scrubbed residual PII from model output",
                severity=logging.WARNING,
                agent_name=callback_context.agent_name,
                pii_categories=cats,
            )
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict[str, Any]]:
        return validate_tool_policy(
            tool.name, tool_args, agent_name=str(tool_context.agent_name)
        )
