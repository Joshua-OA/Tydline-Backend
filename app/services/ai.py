"""
AI-powered services using OpenAI GPT-4o:
- draft_logistics_alert: turn shipment/risk context into a human-readable alert
- extract_email_shipment_data: parse an inbound email and return structured
  container numbers, BL numbers, carrier, and a short summary
- extract_image_shipment_data: extract shipment data from an image (photo of a BL,
  cargo document, etc.) using GPT-4o vision
"""

import json
import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"


def _openai_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }


async def extract_email_shipment_data(
    subject: str,
    body: str,
) -> dict[str, Any] | None:
    """
    Use OpenAI GPT-4o to extract structured shipment data from an inbound email.

    Returns a dict with:
      container_numbers: list[str]  — ISO 6346 container numbers
      bl_numbers:        list[str]  — Bill of Lading / booking reference numbers
      carrier:           str | None — shipping line if identifiable
      summary:           str        — one-sentence summary of the email

    Returns None if OpenAI is not configured or the call fails.
    """
    if not settings.openai_api_key:
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

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                OPENAI_API_URL,
                headers=_openai_headers(),
                json={
                    "model": settings.openai_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 512,
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("openai extract_email_shipment_data failed: %s", e)
        return None

    try:
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        logger.warning("openai email extraction response parse failed: %s", e)
        return None


_VISION_EXTRACTION_PROMPT = (
    "You are a logistics data extractor. Analyse this shipping document image "
    "(it may be a bill of lading, cargo manifest, container arrival notice, or any "
    "other shipping paperwork) and return a JSON object with exactly these keys:\n"
    '  "container_numbers": list of shipping container numbers (ISO 6346 format: '
    "4 letters + 7 digits, e.g. MSCU1234567)\n"
    '  "bl_numbers": list of Bill of Lading numbers, booking references, or shipment '
    "reference numbers\n"
    '  "carrier": the shipping line or carrier name, or null if not found\n'
    '  "summary": one sentence describing what this document shows\n\n'
    "Return ONLY valid JSON. Use empty lists if nothing is found."
)


async def extract_image_shipment_data(
    base64_image: str,
    mime_type: str,
    caption: str | None = None,
) -> dict[str, Any] | None:
    """
    Use OpenAI GPT-4o vision to extract structured shipment data from an image.

    Handles photos of bills of lading, cargo documents, container notices, etc.
    Returns the same shape as extract_email_shipment_data:
      container_numbers, bl_numbers, carrier, summary.
    Returns None if OpenAI is not configured or the call fails.
    """
    if not settings.openai_api_key:
        return None

    content: list[dict] = []
    text_node = _VISION_EXTRACTION_PROMPT
    if caption:
        text_node = f"Caption from sender: {caption}\n\n{_VISION_EXTRACTION_PROMPT}"
    content.append({"type": "text", "text": text_node})
    content.append({
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{base64_image}"},
    })

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                OPENAI_API_URL,
                headers=_openai_headers(),
                json={
                    "model": settings.openai_model,
                    "messages": [{"role": "user", "content": content}],
                    "max_tokens": 512,
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("openai extract_image_shipment_data failed: %s", e)
        return None

    try:
        raw = data["choices"][0]["message"]["content"]
        return json.loads(raw)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        logger.warning("openai image extraction response parse failed: %s", e)
        return None


async def draft_logistics_alert(context: dict[str, Any]) -> str | None:
    """
    Use OpenAI GPT-4o to turn shipment + risk context into a short, clear alert.
    Returns None if OpenAI is not configured or the request fails.
    """
    if not settings.openai_api_key:
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

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                OPENAI_API_URL,
                headers=_openai_headers(),
                json={
                    "model": settings.openai_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 256,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("openai draft_logistics_alert failed: %s", e)
        return None

    try:
        content = data["choices"][0]["message"]["content"]
        return content.strip() if content else None
    except (KeyError, IndexError, TypeError):
        return None
