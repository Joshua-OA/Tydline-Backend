"""
OCR service — extract Bill of Lading data from uploaded PDF or image files.

Flow:
  1. Accept file bytes + mime type
  2. PDF  → extract text with pdfplumber (no OCR needed if text layer exists)
     Image → encode as base64 and send to GPT-4o vision
  3. Pass extracted text/image to GPT-4o to parse container number, BL number, carrier
  4. Return structured result to caller
"""

import base64
import io
import json
import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

_EXTRACTION_PROMPT = (
    "You are a logistics document parser. Extract shipment data from this Bill of Lading document "
    "and return a JSON object with exactly these keys:\n"
    '  "container_number": the ISO 6346 container number (4 letters + 7 digits, e.g. MSCU1234567) or null\n'
    '  "bill_of_lading": the Bill of Lading number or booking reference or null\n'
    '  "carrier": the shipping line or carrier name or null\n'
    '  "shipper": shipper/sender name or null\n'
    '  "consignee": consignee/receiver name or null\n'
    '  "port_of_loading": port of loading or null\n'
    '  "port_of_discharge": port of discharge or null\n\n'
    "Return ONLY valid JSON. Use null for any field not found in the document."
)


def _openai_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }


async def _extract_from_text(text: str) -> dict[str, Any] | None:
    """Send extracted PDF text to GPT-4o for structured parsing."""
    prompt = f"{_EXTRACTION_PROMPT}\n\nDocument text:\n{text[:4000]}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
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
            content = resp.json()["choices"][0]["message"]["content"]
            return json.loads(content)
    except Exception as e:
        logger.warning("OCR text extraction failed: %s", e)
        return None


async def _extract_from_image(image_bytes: bytes, mime_type: str) -> dict[str, Any] | None:
    """Send image to GPT-4o vision for structured parsing."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _EXTRACTION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
            ],
        }
    ]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                OPENAI_API_URL,
                headers=_openai_headers(),
                json={
                    "model": settings.openai_model,
                    "messages": messages,
                    "max_tokens": 512,
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return json.loads(content)
    except Exception as e:
        logger.warning("OCR image extraction failed: %s", e)
        return None


async def extract_bl_from_file(
    file_bytes: bytes,
    mime_type: str,
) -> dict[str, Any] | None:
    """
    Main entry point. Accepts file bytes and mime type.
    Returns extracted BL data dict or None on failure.
    """
    if not settings.openai_api_key:
        logger.warning("OpenAI API key not configured — OCR unavailable")
        return None

    if mime_type == "application/pdf":
        try:
            import pdfplumber

            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                text = "\n".join(
                    page.extract_text() or "" for page in pdf.pages
                ).strip()
            if text:
                return await _extract_from_text(text)
            # PDF has no text layer — fall through to vision
            logger.info("PDF has no text layer, falling back to GPT-4o vision")
            return await _extract_from_image(file_bytes, mime_type)
        except ImportError:
            logger.warning("pdfplumber not installed — falling back to vision")
            return await _extract_from_image(file_bytes, mime_type)
        except Exception as e:
            logger.warning("PDF text extraction failed: %s", e)
            return await _extract_from_image(file_bytes, mime_type)

    # Image files (JPG, PNG, WEBP)
    if mime_type in ("image/jpeg", "image/png", "image/webp"):
        return await _extract_from_image(file_bytes, mime_type)

    logger.warning("Unsupported file type for OCR: %s", mime_type)
    return None
