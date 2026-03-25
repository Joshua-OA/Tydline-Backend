"""
Inbound email ingestion: parse Postmark webhook payload, identify the user,
extract container numbers, link to shipments, persist, and feed Mem0.

Flow:
  1. Postmark receives a forwarded/CC'd email at the Tydline inbound address.
  2. Postmark POSTs the parsed payload to /api/v1/email/inbound.
  3. We match the sender (From) against users.email.
  4. We extract ISO 6346 container numbers from subject + body.
  5. We link found containers to the matched user's shipments.
  6. We store an InboundEmail record.
  7. We feed the email context into Mem0 so the agent can reference it later.
"""

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.memory import add_memory
from app.models.orm import InboundEmail, Shipment, User, UserAuthorizedEmail

logger = logging.getLogger(__name__)

# Regex fallback (used when Groq is not configured)
_CONTAINER_RE = re.compile(r"\b([A-Z]{4}\d{7})\b")
_BL_RE = re.compile(
    r"(?:B/?L|Bill\s+of\s+Lading|BOL|Booking\s+(?:No\.?|Number|Ref\.?))"
    r"[:\s#.\-]*([A-Z0-9]{6,20})",
    re.IGNORECASE,
)

_MEM0_BODY_LIMIT = 1500


def _regex_extract(subject: str, body: str) -> tuple[list[str], list[str]]:
    """Fallback regex extraction when AI is unavailable."""
    text = f"{subject} {body}".upper()
    containers = list(dict.fromkeys(_CONTAINER_RE.findall(text)))
    bls = list(dict.fromkeys(m.upper() for m in _BL_RE.findall(f"{subject} {body}")))
    return containers, bls


def _build_mem0_messages(
    email: InboundEmail,
    bl_numbers: list[str],
    carrier: str | None,
    summary: str | None,
) -> list[dict[str, str]]:
    """
    Build a user/assistant message pair for Mem0.

    Mem0 extracts facts from conversational turns — user/assistant framing
    produces clean, retrievable facts. The user message gives Mem0 the context
    prompt; the assistant message states the facts clearly so Mem0 extracts:
    - container numbers
    - bill of lading numbers
    - carrier
    - what the email was about
    """
    containers = ", ".join(email.container_numbers) if email.container_numbers else "none"
    bls = ", ".join(bl_numbers) if bl_numbers else "none"

    user_msg = (
        f"I forwarded a shipping email to Tydline. "
        f"Subject: {email.subject or '(no subject)'}. "
        f"From: {email.from_name or email.from_email}."
    )

    facts = [f"The user received a shipping email with subject: {email.subject or '(no subject)'}."]
    if containers != "none":
        facts.append(f"Container numbers mentioned: {containers}.")
    if bls != "none":
        facts.append(f"Bill of Lading numbers: {bls}.")
    if carrier:
        facts.append(f"Carrier/shipping line: {carrier}.")
    if summary:
        facts.append(summary)

    assistant_msg = " ".join(facts)

    return [
        {"role": "user", "content": user_msg},
        {"role": "assistant", "content": assistant_msg},
    ]


def _normalize_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize inbound webhook payloads from Resend or Postmark into a
    common internal format.

    Resend wraps the email inside a "data" key and uses snake_case fields.
    Postmark sends the email at the top level with PascalCase fields.
    """
    # Resend: { "type": "email.received", "data": { "from": ..., "to": [...] } }
    if "data" in raw and isinstance(raw["data"], dict):
        data = raw["data"]
        raw_from = data.get("from") or ""
        # Parse "Name <email>" into name + address
        from_name: str | None = None
        from_email: str = raw_from.strip().lower()
        if "<" in raw_from:
            parts = raw_from.split("<", 1)
            from_name = parts[0].strip().strip('"') or None
            from_email = parts[1].rstrip(">").strip().lower()

        # to can be a list or a single string
        raw_to = data.get("to") or []
        if isinstance(raw_to, list):
            to_email = ", ".join(raw_to)
            to_full = [{"Email": addr.strip()} for addr in raw_to]
        else:
            to_email = str(raw_to)
            to_full = [{"Email": to_email}]

        raw_cc = data.get("cc") or []
        cc_full = [{"Email": addr.strip()} for addr in (raw_cc if isinstance(raw_cc, list) else [])]

        return {
            "_from_email": from_email,
            "_from_name": from_name,
            "_to_email": to_email,
            "_to_full": to_full,
            "_cc_full": cc_full,
            "_subject": (data.get("subject") or "").strip(),
            "_body_text": (data.get("text") or "").strip(),
            "_body_html": (data.get("html") or "").strip() or None,
            "_message_id": (data.get("message_id") or "").strip() or None,
        }

    # Postmark: top-level PascalCase fields
    raw_from = raw.get("From") or ""
    from_name = (raw.get("FromName") or "").strip() or None
    from_email = raw_from.strip().lower()
    if "<" in from_email:
        from_email = from_email.split("<")[-1].rstrip(">").strip()

    return {
        "_from_email": from_email,
        "_from_name": from_name,
        "_to_email": (raw.get("To") or "").strip(),
        "_to_full": raw.get("ToFull") or [],
        "_cc_full": raw.get("CcFull") or [],
        "_subject": (raw.get("Subject") or "").strip(),
        "_body_text": (raw.get("TextBody") or "").strip(),
        "_body_html": (raw.get("HtmlBody") or "").strip() or None,
        "_message_id": (raw.get("MessageID") or "").strip() or None,
    }


async def process_inbound_email(
    session: AsyncSession,
    payload: dict[str, Any],
) -> InboundEmail:
    """
    Parse an inbound webhook payload from Resend or Postmark, match user,
    extract container numbers, link to shipments, persist, and feed Mem0.
    """
    n = _normalize_payload(payload)
    from_email = n["_from_email"]
    from_name = n["_from_name"]
    to_email = n["_to_email"]
    subject = n["_subject"]
    body_text = n["_body_text"]
    body_html = n["_body_html"]
    message_id = n["_message_id"]

    # ------------------------------------------------------------------
    # 1. Deduplicate by MessageID
    # ------------------------------------------------------------------
    if message_id:
        existing_result = await session.execute(
            select(InboundEmail).where(InboundEmail.message_id == message_id)
        )
        existing_record = existing_result.scalar_one_or_none()
        if existing_record is not None:
            logger.info("Duplicate inbound email %s — skipping", message_id)
            return existing_record

    # ------------------------------------------------------------------
    # 2. Match to a registered user
    #    Primary:  to_email  → users.tracking_email  (company's tracking address)
    #    Fallback: from_email → users.email           (legacy / direct sender match)
    # ------------------------------------------------------------------
    user: User | None = None
    match_method: str = "none"

    def _bare(addr: str) -> str:
        addr = addr.strip().lower()
        if "<" in addr:
            addr = addr.split("<")[-1].rstrip(">").strip()
        return addr

    # Collect all recipient addresses from To, ToFull, Cc, CcFull
    all_recipient_addresses: list[str] = []
    for entry in n["_to_full"]:
        if isinstance(entry, dict) and entry.get("Email"):
            all_recipient_addresses.append(_bare(entry["Email"]))
    for entry in n["_cc_full"]:
        if isinstance(entry, dict) and entry.get("Email"):
            all_recipient_addresses.append(_bare(entry["Email"]))
    # Also include the raw To string as fallback
    if to_email:
        all_recipient_addresses.append(_bare(to_email))

    # 1. Try to match any recipient address against users.tracking_email
    for addr in all_recipient_addresses:
        if not addr:
            continue
        result = await session.execute(select(User).where(User.tracking_email == addr))
        user = result.scalar_one_or_none()
        if user is not None:
            match_method = "tracking_email"
            logger.info("Inbound email matched user %s via tracking_email <%s>", user.id, addr)
            break

    # 2. Check from_email against user_authorized_emails
    if user is None and from_email:
        auth_result = await session.execute(
            select(UserAuthorizedEmail).where(UserAuthorizedEmail.email == from_email)
        )
        auth_entry = auth_result.scalar_one_or_none()
        if auth_entry:
            user_result = await session.execute(select(User).where(User.id == auth_entry.user_id))
            user = user_result.scalar_one_or_none()
            if user:
                match_method = "authorized_email"
                logger.info("Inbound email matched user %s via authorized_email <%s>", user.id, from_email)

    # 3. Fall back to from_email → users.email
    if user is None and from_email:
        result = await session.execute(select(User).where(User.email == from_email))
        user = result.scalar_one_or_none()
        if user is None:
            logger.info("Inbound email from unregistered sender <%s>", from_email)
        else:
            match_method = "from_email"
            logger.info("Inbound email matched user %s via from_email <%s>", user.id, from_email)

    # ------------------------------------------------------------------
    # 3. AI extraction of containers, BL numbers, carrier, and summary
    #    Falls back to regex if Groq is not configured or the call fails.
    # ------------------------------------------------------------------
    from app.services.ai import extract_email_shipment_data

    carrier: str | None = None
    email_summary: str | None = None
    ai_result = await extract_email_shipment_data(subject, body_text)

    if ai_result:
        container_numbers = [c.upper() for c in (ai_result.get("container_numbers") or [])]
        bl_numbers = [b.upper() for b in (ai_result.get("bl_numbers") or [])]
        carrier = ai_result.get("carrier") or None
        email_summary = ai_result.get("summary") or None
        logger.info("AI extracted — containers: %s, BLs: %s, carrier: %s", container_numbers, bl_numbers, carrier)
    else:
        container_numbers, bl_numbers = _regex_extract(subject, body_text)
        logger.info("Regex fallback — containers: %s, BLs: %s", container_numbers, bl_numbers)

    # ------------------------------------------------------------------
    # 4. Link containers to the user's shipments (by container number or BL)
    # ------------------------------------------------------------------
    matched_shipment_ids: list[str] = []
    new_shipment_ids: list[str] = []
    if user and (container_numbers or bl_numbers):
        filters = []
        if container_numbers:
            filters.append(Shipment.container_number.in_(container_numbers))
        if bl_numbers:
            filters.append(Shipment.bill_of_lading.in_(bl_numbers))

        from sqlalchemy import or_
        result = await session.execute(
            select(Shipment).where(Shipment.user_id == user.id, or_(*filters))
        )
        shipments = list(result.scalars().all())
        matched_shipment_ids = [str(s.id) for s in shipments]
        if matched_shipment_ids:
            logger.info("Email matched existing shipments: %s", matched_shipment_ids)

        # Create shipments for new container numbers
        existing_containers = {s.container_number for s in shipments if s.container_number}
        existing_bls = {s.bill_of_lading for s in shipments if s.bill_of_lading}

        for container in container_numbers:
            if container not in existing_containers:
                bl = bl_numbers[0] if bl_numbers else None
                new_shipment = Shipment(
                    container_number=container,
                    bill_of_lading=bl,
                    carrier=carrier,
                    user_id=user.id,
                    status="pending_approval",
                )
                session.add(new_shipment)
                await session.flush()
                new_shipment_ids.append(str(new_shipment.id))
                logger.info("Created shipment %s (pending_approval) for container %s from inbound email", new_shipment.id, container)

        # BL-only shipments — no container number identified yet
        if not container_numbers:
            for bl in bl_numbers:
                if bl not in existing_bls:
                    new_shipment = Shipment(
                        container_number=None,
                        bill_of_lading=bl,
                        carrier=carrier,
                        user_id=user.id,
                        status="pending_approval",
                    )
                    session.add(new_shipment)
                    await session.flush()
                    new_shipment_ids.append(str(new_shipment.id))
                    logger.info("Created shipment %s (pending_approval) for BL %s from inbound email", new_shipment.id, bl)

        if new_shipment_ids:
            matched_shipment_ids.extend(new_shipment_ids)

    # ------------------------------------------------------------------
    # 5. Persist InboundEmail record — each extracted field in its own column
    # ------------------------------------------------------------------
    record = InboundEmail(
        user_id=user.id if user else None,
        from_email=from_email,
        from_name=from_name,
        to_email=to_email,
        subject=subject or None,
        body_text=body_text or None,
        body_html=body_html,
        message_id=message_id,
        container_numbers=container_numbers or None,
        bl_numbers=bl_numbers or None,
        carrier=carrier,
        email_summary=email_summary,
        matched_shipment_ids=matched_shipment_ids or None,
        mem0_stored=False,
    )
    session.add(record)
    await session.flush()

    # ------------------------------------------------------------------
    # 6. Feed into Mem0 so the agent can reference emails in conversation
    # ------------------------------------------------------------------
    if user:
        context = _build_mem0_messages(record, bl_numbers, carrier, email_summary)
        try:
            await add_memory(
                str(user.id),
                context,
                metadata={
                    "source": "inbound_email",
                    "email_id": str(record.id),
                    "containers": container_numbers,
                    "bl_numbers": bl_numbers,
                    "matched_shipments": matched_shipment_ids,
                },
            )
            record.mem0_stored = True
            logger.info("Mem0 updated for user %s from email %s", user.id, record.id)
        except Exception:
            logger.warning("Mem0 update failed for email %s — continuing", record.id)

    await session.commit()
    await session.refresh(record)

    # ------------------------------------------------------------------
    # 7. Send confirmation email to the user
    # ------------------------------------------------------------------
    if user and new_shipment_ids:
        try:
            await _send_shipment_added_email(
                to=user.email,
                subject=subject or "(no subject)",
                container_numbers=container_numbers,
                bl_numbers=bl_numbers,
                carrier=carrier,
            )
        except Exception:
            logger.warning("Shipment confirmation email failed for user %s — continuing", user.id)

    return record


async def _send_shipment_added_email(
    to: str,
    subject: str,
    container_numbers: list[str],
    bl_numbers: list[str],
    carrier: str | None,
) -> None:
    from pathlib import Path
    from app.core.config import settings
    from app.services.email import send_email

    template_path = Path(__file__).parent.parent.parent / "emails" / "shipment-added.html"
    html = template_path.read_text()

    dashboard_url = f"{settings.frontend_url}/dashboard/approvals"

    html = html.replace("{{subject}}", subject)
    html = html.replace("{{dashboard_url}}", dashboard_url)
    html = html.replace("{{carrier}}", carrier or "")

    if bl_numbers:
        html = html.replace("{{bl_numbers}}", "<br>".join(bl_numbers))
        html = html.replace("{{#if bl_numbers}}", "").replace("{{/if}}", "")
    else:
        # Remove the BL block
        import re as _re
        html = _re.sub(r"\{\{#if bl_numbers\}\}.*?\{\{/if\}\}", "", html, flags=_re.DOTALL)

    if container_numbers:
        html = html.replace("{{container_numbers}}", "<br>".join(container_numbers))
        html = html.replace("{{#if container_numbers}}", "").replace("{{/if}}", "")
    else:
        import re as _re
        html = _re.sub(r"\{\{#if container_numbers\}\}.*?\{\{/if\}\}", "", html, flags=_re.DOTALL)

    if carrier:
        html = html.replace("{{#if carrier}}", "").replace("{{/if}}", "")
    else:
        import re as _re
        html = _re.sub(r"\{\{#if carrier\}\}.*?\{\{/if\}\}", "", html, flags=_re.DOTALL)

    items = []
    if bl_numbers:
        items.append("BL: " + ", ".join(bl_numbers))
    if container_numbers:
        items.append("Containers: " + ", ".join(container_numbers))
    text_body = f"Shipments added to your Tydline account (pending approval):\n\n" + "\n".join(items) + f"\n\nApprove at: {dashboard_url}"

    await send_email(to=to, subject="Shipments added — pending approval", text_body=text_body, html_body=html)
