"""
imap_service.py
Polls monitored IMAP mailboxes for new (UNSEEN) emails and ingests them
as EmailRequest records with status='received'.
"""
import hashlib
import imaplib
import email as email_lib
import logging
import re
import secrets
from datetime import datetime, timezone
from email.header import decode_header
from typing import List
import asyncio

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

import os

from app.models.models import MonitoredEmail, EmailRequest, AuditLog
from app.api.monitored_emails import decrypt_password

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_header_value(value: str) -> str:
    parts = decode_header(value or "")
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="replace")
        else:
            result += str(part)
    return result.strip()


def _extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if (part.get_content_type() == "text/plain"
                    and "attachment" not in str(part.get("Content-Disposition", ""))):
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def _parse_from(raw: str):
    """Return (from_name, from_email) from a raw From header."""
    m = re.match(r'^(.+?)\s*<([^>]+)>', raw.strip())
    if m:
        return m.group(1).strip().strip('"'), m.group(2).strip()
    return "", raw.strip()


# ---------------------------------------------------------------------------
# Sync IMAP fetch (runs in executor)
# ---------------------------------------------------------------------------

def _subject_matches(subject: str, filters: list[str]) -> bool:
    """Return True if subject contains any filter keyword (case-insensitive).
    Empty filters = accept all (no filtering configured)."""
    if not filters:
        return True
    lower = subject.lower()
    return any(f.lower() in lower for f in filters)


def _extract_document_attachments(msg) -> list[dict]:
    """
    Return list of {filename, bytes} for each document attachment in the message.
    Accepts PDFs, images, and office documents — anything the conversion service
    can handle.  FTP downloads and order uploads are intentionally excluded here.
    """
    from app.services.pdf_conversion_service import SCANNABLE_EXTENSIONS
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition  = str(part.get("Content-Disposition", ""))
            filename     = part.get_filename()
            if filename:
                filename = _decode_header_value(filename)

            is_pdf        = content_type == "application/pdf" or (filename and filename.lower().endswith(".pdf"))
            is_image      = content_type.startswith("image/")
            has_doc_ext   = filename and (
                "." in filename and
                ("." + filename.rsplit(".", 1)[-1]).lower() in SCANNABLE_EXTENSIONS
            )

            if (is_pdf or is_image or has_doc_ext):
                if "attachment" in disposition or is_pdf or is_image or has_doc_ext:
                    payload = part.get_payload(decode=True)
                    if payload:
                        attachments.append({
                            "filename": filename or "document.pdf",
                            "bytes":    payload,
                        })
    return attachments


def _poll_mailbox_sync(
    imap_host: str,
    imap_port: int,
    imap_user: str,
    password: str,
    use_ssl: bool,
    folder: str,
    subject_filters: list[str],
    oauth_access_token: str | None = None,
) -> List[dict]:
    """Connect to IMAP, fetch UNSEEN emails, ingest matching ones, leave others UNSEEN."""
    conn = imaplib.IMAP4_SSL(imap_host, imap_port) if use_ssl else imaplib.IMAP4(imap_host, imap_port)
    results = []
    try:
        if oauth_access_token:
            auth_string = f"user={imap_user}\x01auth=Bearer {oauth_access_token}\x01\x01"
            conn.authenticate("XOAUTH2", lambda _x: auth_string.encode())
        else:
            conn.login(imap_user, password)
        conn.select(folder)
        status, data = conn.search(None, "UNSEEN")
        if status != "OK" or not data[0]:
            return results

        for msg_num in data[0].split():
            # BODY.PEEK[] fetches without auto-marking as Seen
            status2, msg_data = conn.fetch(msg_num, "(BODY.PEEK[])")
            if status2 != "OK":
                continue
            msg = email_lib.message_from_bytes(msg_data[0][1])

            subject     = _decode_header_value(msg.get("Subject", "(no subject)"))
            from_raw    = msg.get("From", "")
            from_name, from_email = _parse_from(from_raw)
            body        = _extract_body(msg) or "(empty)"
            message_id  = (msg.get("Message-ID") or "").strip()
            if not message_id:
                # Generate a stable synthetic ID so deduplication still works
                _raw = f"{from_email}|{subject}|{msg.get('Date', '')}"
                message_id = f"<synthetic-{hashlib.sha256(_raw.encode()).hexdigest()[:32]}@imap-dedup>"
            in_reply_to = (msg.get("In-Reply-To") or "").strip()
            # References contains the full chain of message IDs in the thread
            references  = [r.strip() for r in (msg.get("References") or "").split() if r.strip()]
            pdf_attachments = _extract_document_attachments(msg)

            # Subject filter check — applies to ALL emails (with or without attachments)
            if not _subject_matches(subject, subject_filters):
                logger.debug("Skipping (no subject match): %s", subject)
                conn.store(msg_num, "+FLAGS", "\\Seen")
                continue

            if pdf_attachments:
                results.append({
                    "from_email":       from_email or from_raw,
                    "from_name":        from_name,
                    "subject":          subject,
                    "body":             body,
                    "pdf_attachments":  pdf_attachments,
                    "is_pod_delivery":  True,
                    "message_id":       message_id,
                    "in_reply_to":      in_reply_to,
                    "references":       references,
                })
                conn.store(msg_num, "+FLAGS", "\\Seen")
                continue

            results.append({
                "from_email":       from_email or from_raw,
                "from_name":        from_name,
                "subject":          subject,
                "body":             body,
                "pdf_attachments":  [],
                "is_pod_delivery":  False,
                "message_id":       message_id,
                "in_reply_to":      in_reply_to,
                "references":       references,
            })
            # Only mark as read if we're actually ingesting it
            conn.store(msg_num, "+FLAGS", "\\Seen")

    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return results


# ---------------------------------------------------------------------------
# Async poll — called by the background loop
# ---------------------------------------------------------------------------


async def _notify_admins_reauth(db: AsyncSession, me: MonitoredEmail):
    """Send email to all admins when a mailbox's OAuth token expires or is revoked."""
    try:
        from sqlalchemy import select
        from app.models.models import User
        from app.services.email_service import send_email

        result = await db.execute(
            select(User).where(User.role.in_(["admin", "super_admin"]), User.is_active == True)
        )
        admins = result.scalars().all()
        if not admins:
            return

        subject = f"Action required: {me.email} mailbox authentication expired"
        body = (
            f"This is an automated alert from the POD Automation System.\n\n"
            f"The monitored mailbox {me.email} has lost its Microsoft 365 authorization.\n"
            f"Email polling for this mailbox has stopped.\n\n"
            f"The mailbox owner has been sent a re-authorization link. "
            f"If they do not act, you can resend the invitation from the admin Settings page.\n\n"
            f"Error: {me.last_error or 'Authentication failed'}"
        )
        for admin in admins:
            await send_email(db, to=admin.email, subject=subject, body=body)
            logger.info("Reauth admin notification sent to %s for mailbox %s", admin.email, me.email)
    except Exception as exc:
        logger.warning("Failed to send reauth admin notification: %s", exc)


async def poll_monitored_email(db: AsyncSession, me: MonitoredEmail) -> int:
    """
    Poll one monitored mailbox. Creates EmailRequest rows for each new email.
    Updates last_checked_at and last_error. Returns count of emails ingested.
    """
    password: str = ""
    oauth_access_token: str | None = None

    if (me.auth_type or "password") == "oauth_microsoft":
        from app.api.oauth_microsoft import get_valid_access_token
        oauth_access_token = await get_valid_access_token(db, me)
        if not oauth_access_token:
            me.last_checked_at = datetime.now()
            await db.commit()
            return 0
    else:
        try:
            password = decrypt_password(me.imap_password)
        except Exception:
            me.last_error = "Failed to decrypt IMAP password"
            await db.commit()
            return 0

    # Load subject filters from system_config
    from app.models.models import SystemConfig
    cfg_row = await db.get(SystemConfig, "imap_subject_filters")
    raw_filters = (cfg_row.value or "") if cfg_row else ""
    subject_filters = [f.strip() for f in raw_filters.split(",") if f.strip()]

    smtp_from_row = await db.get(SystemConfig, "smtp_from")
    smtp_from_addr = (smtp_from_row.value or "").strip().lower() if smtp_from_row else ""

    loop = asyncio.get_event_loop()
    args = (
        me.imap_host,
        me.imap_port or 993,
        me.imap_user or me.email,
        password,
        me.use_ssl if me.use_ssl is not None else True,
        me.mailbox_folder or "INBOX",
        subject_filters,
        oauth_access_token,
    )
    emails = None
    last_exc = None
    for attempt in range(2):  # one retry on transient EOF/socket errors
        try:
            emails = await loop.run_in_executor(None, _poll_mailbox_sync, *args)
            break
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                logger.warning("IMAP poll attempt 1 failed for %s: %s — retrying", me.email, exc)
                await asyncio.sleep(5)

    if emails is None:
        me.last_checked_at = datetime.now()
        me.last_error = str(last_exc)
        msg = str(last_exc).upper() if last_exc else ""
        if (me.auth_type or "password") == "oauth_microsoft" and (
            "AUTHENTICATIONFAILED" in msg
            or "INVALID CREDENTIALS" in msg
            or "XOAUTH2" in msg
        ):
            prev_status = me.status
            me.status = "reauth_required"
            await db.commit()
            logger.error("IMAP poll failed for %s: %s", me.email, last_exc)

            # Notify admins when a mailbox transitions to reauth_required
            if prev_status != "reauth_required":
                await _notify_admins_reauth(db, me)
        else:
            me.status = "error"
            await db.commit()
            logger.error("IMAP poll failed for %s: %s", me.email, last_exc)
        return 0

    me.last_checked_at = datetime.now()
    me.last_error = None
    if me.status == "error":
        me.status = "active"

    def _clean(s: str) -> str:
        """Strip null bytes and other chars PostgreSQL UTF-8 won't accept."""
        return s.replace("\x00", "") if s else s

    count = 0
    request_ids = []
    for em in emails:
        # Skip emails sent by the system to itself (prevents notification loops)
        if smtp_from_addr and em.get("from_email", "").strip().lower() == smtp_from_addr:
            logger.info("Skipping self-sent email (from %s): %s", em["from_email"], em.get("subject"))
            continue

        if em.get("is_pod_delivery") and em.get("pdf_attachments"):
            # This is an incoming email with PDF attachments — check if it's a
            # carrier POD reply. If nothing matched, fall through to normal processing.
            handled = await _handle_pod_reply(db, em)
            if handled:
                count += 1
                continue

        # Deduplicate by IMAP Message-ID — skip if already ingested
        msg_id = em.get("message_id") or None
        if msg_id:
            existing = await db.execute(
                select(EmailRequest).where(EmailRequest.imap_message_id == msg_id)
            )
            if existing.scalar_one_or_none():
                logger.info("Skipping duplicate email (Message-ID already ingested): %s", msg_id)
                continue

        # Resolve the best thread ancestor we know about.
        # Check In-Reply-To first; if not in our DB, walk References (most recent first)
        # to find a message ID we did ingest. This handles multi-hop replies where the
        # customer replies to our outgoing response (which has a different Message-ID).
        in_reply_to = (em.get("in_reply_to") or "").strip() or None
        references  = em.get("references") or []
        logger.info(
            "Thread resolution for '%s' — in_reply_to=%r  references(%d)=%r",
            em["subject"][:80], in_reply_to, len(references), references,
        )
        thread_candidates = ([in_reply_to] if in_reply_to else []) + list(reversed(references))
        resolved_thread_id = None
        for candidate in thread_candidates:
            if not candidate:
                continue
            row = await db.execute(
                select(EmailRequest).where(
                    or_(
                        EmailRequest.imap_message_id == candidate,
                        EmailRequest.smtp_message_id == candidate,
                    )
                )
            )
            match = row.scalar_one_or_none()
            logger.info("  thread candidate %r → %s", candidate, "MATCH" if match else "no match")
            if match:
                resolved_thread_id = candidate
                break
        logger.info(
            "Thread resolution result for '%s': resolved_thread_id=%r",
            em["subject"][:80], resolved_thread_id,
        )

        ref = f"REQ-{secrets.token_hex(4).upper()}"
        req = EmailRequest(
            reference_number=ref,
            from_email=_clean(em["from_email"])[:200],
            from_name=(_clean(em["from_name"] or ""))[:200] or None,
            subject=_clean(em["subject"]),
            body=_clean(em["body"]),
            status="received",
            imap_message_id=msg_id,
            imap_in_reply_to=resolved_thread_id,
        )
        db.add(req)
        await db.flush()  # get req.id assigned before creating audit log
        audit = AuditLog(
            request_id=req.id,
            action="email_received",
            actor="imap_poller",
            summary=f"Email received from {em['from_email']}: {em['subject'][:100]}",
            detail={"from_email": em["from_email"], "from_name": em["from_name"],
                    "subject": em["subject"], "monitored_inbox": me.email},
        )
        db.add(audit)
        request_ids.append(str(req.id))
        count += 1
        logger.info("Ingested email from %s: %s", em["from_email"], em["subject"])

    await db.commit()

    # Enqueue each customer email for processing via Celery
    from app.core.tasks import process_email_task
    for req_id in request_ids:
        process_email_task.delay(req_id)

    return count


async def _handle_pod_reply(db: AsyncSession, em: dict) -> bool:
    """
    Process an incoming email that has PDF attachments — treat it as a carrier
    POD delivery. Extract delivery numbers from subject/body, save the PDFs,
    and resume any awaiting_pod pipeline requests.

    Returns True if at least one attachment was saved (email was handled).
    Returns False if nothing matched — caller should process via normal pipeline.
    """
    from app.services.pod_folder_service import save_pod_bytes
    from app.models.models import PodRegistry, EmailRequest as EmailRequestModel, SystemConfig
    from sqlalchemy import or_

    subject = em.get("subject", "")
    body    = em.get("body", "")
    text    = subject + " " + body

    # Extract delivery / order numbers from the email text
    delivery_matches = re.findall(r'DEL-[\w-]+', text, re.I)
    order_matches    = re.findall(r'ORD-\d+', text, re.I)
    all_refs = list(dict.fromkeys([m.upper() for m in delivery_matches + order_matches]))

    logger.info("POD reply from %s — refs=%s, attachments=%d",
                em["from_email"], all_refs, len(em["pdf_attachments"]))

    resumed_request_ids = []
    any_saved = False

    from app.services.pdf_conversion_service import convert_bytes_to_pdf

    for att in em["pdf_attachments"]:
        # Convert to PDF if the attachment is not already a PDF
        att_bytes    = att["bytes"]
        att_filename = att["filename"]
        try:
            att_bytes, att_filename = await convert_bytes_to_pdf(att_bytes, att_filename)
        except Exception as conv_err:
            logger.warning("POD reply: could not convert %s to PDF: %s - skipping", att_filename, conv_err)
            continue

        saved_filename = None
        matched_reg    = None

        # Try to match a registry entry for one of the delivery/order refs
        for ref in all_refs:
            r = await db.execute(
                select(PodRegistry).where(
                    or_(
                        PodRegistry.delivery_number == ref,
                        PodRegistry.customer_po == ref,
                    )
                )
            )
            reg = r.scalar_one_or_none()
            if reg:
                matched_reg = reg
                break

        # If no match by refs, look for the oldest manual_required entry
        if not matched_reg:
            r = await db.execute(
                select(PodRegistry)
                .where(PodRegistry.status == "manual_required")
                .order_by(PodRegistry.created_at.asc())
                .limit(1)
            )
            matched_reg = r.scalar_one_or_none()

        if matched_reg:
            delivery = matched_reg.delivery_number
            order_id = str(matched_reg.order_id) if matched_reg.order_id else None
            saved_filename = await save_pod_bytes(
                db=db,
                file_bytes=att_bytes,
                delivery_number=delivery,
                original_filename=att_filename,
                received_via="email",
                matched_by="carrier_reply",
                order_id=order_id,
                customer_po=matched_reg.customer_po,
            )
            logger.info("Saved carrier POD %s for delivery %s", saved_filename, delivery)
            any_saved = True

            # Find an awaiting_pod EmailRequest for this order
            if order_id:
                r2 = await db.execute(
                    select(EmailRequestModel).where(
                        EmailRequestModel.order_id == matched_reg.order_id,
                        EmailRequestModel.status == "awaiting_pod",
                    ).order_by(EmailRequestModel.received_at.desc()).limit(1)
                )
                waiting_req = r2.scalar_one_or_none()
                if waiting_req and str(waiting_req.id) not in resumed_request_ids:
                    resumed_request_ids.append(str(waiting_req.id))
        elif all_refs:
            # No registry entry but we extracted a reference — save with that ref
            saved_filename = await save_pod_bytes(
                db=db,
                file_bytes=att_bytes,
                delivery_number=all_refs[0],
                original_filename=att_filename,
                received_via="email",
                matched_by="carrier_reply",
            )
            logger.info("Saved carrier POD %s with ref %s (no registry match)", saved_filename, all_refs[0])
            any_saved = True
        else:
            # No registry match AND no extractable reference — skip to avoid
            # phantom UNKNOWN entries. The email will be processed normally.
            logger.info("Skipping PDF attachment %s — no delivery/order reference found", att_filename)

    if any_saved:
        await db.commit()

    # Enqueue resume tasks for any waiting requests
    from app.core.tasks import resume_pod_task
    for req_id in resumed_request_ids:
        resume_pod_task.delay(req_id)
        logger.info("Enqueued resume_pod_task for request %s", req_id)

    return any_saved
