"""
AI-powered services using Groq:
- draft_logistics_alert: turn shipment/risk context into a human-readable alert
- extract_email_shipment_data: parse an inbound email and return structured
  container numbers, BL numbers, carrier, and a short summary

Every call is traced in Langfuse when configured.
"""

import json
import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


async def extract_email_shipment_data(
    subject: str,
    body: str,
) -> dict[str, Any] | None:
    """
    Use Groq to extract structured shipment data from an inbound email.

    Returns a dict with:
      container_numbers: list[str]  — ISO 6346 container numbers
      bl_numbers:        list[str]  — Bill of Lading / booking reference numbers
      carrier:           str | None — shipping line if identifiable
      summary:           str        — one-sentence summary of the email

    Returns None if Groq is not configured or the call fails; callers should
    fall back to regex extraction in that case.
    """
    if not settings.groq_api_key:
        return None

    prompt = (
        "You are a logistics data extractor. Analyse the shipping email below and return "
        "a JSON object with exactly these keys:\n"
        '  "container_numbers": list of shipping container numbers (ISO 6346 format: 4 letters + 7 digits, e.g. MSCU1234567)\n'
        '  "bl_numbers": list of Bill of Lading numbers, booking references, or shipment reference numbers\n'
        '  "carrier": the shipping line or carrier name, or null if not found\n'
        '  "summary": one sentence describing what this email is about\n\n'
        "Return ONLY valid JSON. Use empty lists if nothing is found.\n\n"
        f"Subject: {subject}\n\n"
        f"Body:\n{body[:3000]}"
    )

    messages = [{"role": "user", "content": prompt}]

    from app.observability.langfuse import create_trace

    trace = create_trace(
        name="email_extraction",
        metadata={"subject": subject[:120]},
        tags=["llm", "email", "extraction"],
    )
    generation = None
    if trace is not None:
        try:
            generation = trace.start_observation(
                name="groq_email_extraction",
                as_type="generation",
                model=settings.groq_model,
                input=messages,
            )
        except Exception:
            pass

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.groq_model,
                    "messages": messages,
                    "max_tokens": 512,
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("groq extract_email_shipment_data failed: %s", e)
        if generation is not None:
            try:
                generation.update(output=None, level="ERROR", status_message=str(e))
                generation.end()
            except Exception:
                pass
        return None

    try:
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        usage = data.get("usage", {})
        if generation is not None:
            try:
                generation.update(
                    output=parsed,
                    usage_details={
                        "input": usage.get("prompt_tokens") or 0,
                        "output": usage.get("completion_tokens") or 0,
                        "total": usage.get("total_tokens") or 0,
                    },
                )
                generation.end()
            except Exception:
                pass
        return parsed
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        logger.warning("groq email extraction response parse failed: %s", e)
        if generation is not None:
            try:
                generation.update(output=None, level="ERROR", status_message=str(e))
                generation.end()
            except Exception:
                pass
        return None


async def draft_logistics_alert(context: dict[str, Any]) -> str | None:
    """
    Use Groq to turn shipment + risk context into a short, clear alert.
    Returns None if Groq is not configured or the request fails.
    """
    if not settings.groq_api_key:
        return None

    container = context.get("container_number", "")
    status = context.get("status", "")
    location = context.get("location") or "unknown"
    eta = context.get("eta")
    free_days = context.get("free_days_remaining")
    risk = context.get("risk_level", "")

    prompt = f"""You are a logistics assistant helping an importer avoid demurrage fees.

Container: {container}
Status: {status}
Location: {location}
ETA: {eta}
Free days remaining: {free_days}
Risk level: {risk}

Write a short, clear alert (2-4 sentences) explaining the situation and what the importer should do next. Be direct and actionable."""

    messages = [{"role": "user", "content": prompt}]

    # --- Langfuse: open trace + generation ---
    from app.observability.langfuse import create_trace

    trace = create_trace(
        name="draft_logistics_alert",
        metadata={"container_number": container, "risk_level": risk},
        tags=["llm", "alert"],
    )
    generation = None
    if trace is not None:
        try:
            generation = trace.start_observation(
                name="groq_logistics_alert",
                as_type="generation",
                model=settings.groq_model,
                input=messages,
            )
        except Exception:
            pass

    # --- Groq call ---
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.groq_model,
                    "messages": messages,
                    "max_tokens": 256,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("groq draft_logistics_alert failed: %s", e)
        if generation is not None:
            try:
                generation.update(output=None, level="ERROR", status_message=str(e))
                generation.end()
            except Exception:
                pass
        return None

    # --- Parse response ---
    try:
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        if generation is not None:
            try:
                generation.update(
                    output=content,
                    usage_details={
                        "input": usage.get("prompt_tokens") or 0,
                        "output": usage.get("completion_tokens") or 0,
                        "total": usage.get("total_tokens") or 0,
                    },
                )
                generation.end()
            except Exception:
                pass
        return content.strip() if content else None
    except (KeyError, IndexError, TypeError):
        if generation is not None:
            try:
                generation.update(output=None, level="ERROR", status_message="unexpected response shape")
                generation.end()
            except Exception:
                pass
        return None
