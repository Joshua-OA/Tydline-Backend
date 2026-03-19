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
from app.models.orm import InboundEmail, Shipment, User

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


async def process_inbound_email(
    session: AsyncSession,
    payload: dict[str, Any],
) -> InboundEmail:
    """
    Parse *payload* (Postmark inbound JSON), match user, extract containers
    and BL numbers, persist InboundEmail, and feed Mem0.

    Postmark inbound fields used:
      From, FromName, To, Subject, TextBody, HtmlBody, MessageID, Headers
    """
    from_email = (payload.get("From") or "").strip().lower()
    to_email = (payload.get("To") or "").strip()
    subject = (payload.get("Subject") or "").strip()
    body_text = (payload.get("TextBody") or "").strip()
    body_html = (payload.get("HtmlBody") or "").strip() or None
    message_id = (payload.get("MessageID") or "").strip() or None
    from_name = (payload.get("FromName") or "").strip() or None
    raw_headers = payload.get("Headers")

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

    # Extract the bare address from to_email (Postmark may include display name)
    to_bare = to_email.strip().lower()
    if "<" in to_bare:
        to_bare = to_bare.split("<")[-1].rstrip(">").strip()

    # 1. Try to match by tracking_email (To field)
    if to_bare:
        result = await session.execute(select(User).where(User.tracking_email == to_bare))
        user = result.scalar_one_or_none()
        if user is not None:
            match_method = "tracking_email"
            logger.info("Inbound email matched user %s via tracking_email <%s>", user.id, to_bare)

    # 2. Fall back to from_email → users.email
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

        # Create shipments for containers not yet tracked
        existing_containers = {s.container_number for s in shipments}
        new_containers = [c for c in container_numbers if c not in existing_containers]
        new_shipment_ids: list[str] = []
        for container in new_containers:
            new_shipment = Shipment(
                container_number=container,
                bill_of_lading=bl_numbers[0] if bl_numbers else None,
                carrier=carrier,
                user_id=user.id,
                status="pending_approval",
            )
            session.add(new_shipment)
            await session.flush()
            new_shipment_ids.append(str(new_shipment.id))
            logger.info("Created shipment %s (pending_approval) for container %s from inbound email", new_shipment.id, container)

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
        raw_headers=raw_headers,
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
    return record
