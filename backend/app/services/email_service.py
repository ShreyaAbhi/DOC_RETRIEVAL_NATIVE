"""
email_service.py
Handles outbound SMTP email (Outlook / Office365 compatible).
"""
import os
import re
import smtplib
import logging
import bleach
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import make_msgid
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.models import SystemConfig


def _strip_html(html: str) -> str:
    """Convert HTML to plain text for fallback."""
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.I)
    text = re.sub(r'<p[^>]*>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()

logger = logging.getLogger(__name__)

# HTML tags and attributes allowed in the email signature (strips <script>, event handlers, etc.)
_SIG_ALLOWED_TAGS = {
    'div', 'span', 'p', 'br', 'hr', 'b', 'i', 'u', 'strong', 'em', 'small',
    'h1', 'h2', 'h3', 'h4', 'ul', 'ol', 'li',
    'table', 'thead', 'tbody', 'tr', 'td', 'th',
    'a', 'img', 'font', 'pre', 'code',
}
_SIG_ALLOWED_ATTRS = {
    '*':     ['style', 'class', 'align', 'valign', 'width', 'height', 'color'],
    'a':     ['href', 'target', 'rel'],
    'img':   ['src', 'alt', 'width', 'height'],
    'table': ['border', 'cellpadding', 'cellspacing'],
    'td':    ['colspan', 'rowspan'],
    'th':    ['colspan', 'rowspan'],
    'font':  ['size', 'face', 'color'],
}

def _sanitize_signature(html: str) -> str:
    """Strip dangerous tags/attributes from the email signature (stored XSS prevention)."""
    return bleach.clean(html, tags=_SIG_ALLOWED_TAGS, attributes=_SIG_ALLOWED_ATTRS, strip=True)


async def _get_smtp_config(db: AsyncSession) -> dict:
    keys = ['smtp_host', 'smtp_port', 'smtp_user', 'smtp_password', 'smtp_from', 'email_signature']
    result = await db.execute(select(SystemConfig).where(SystemConfig.key.in_(keys)))
    rows = {r.key: r.value for r in result.scalars().all()}
    return rows


async def send_email(
    db: AsyncSession,
    to: str,
    subject: str,
    body: str,
    attachments: Optional[List[str]] = None,  # list of file paths
    reply_to: Optional[str] = None,
) -> dict:
    """
    Send an email. Returns {'sent': True/False, 'message_id': ..., 'simulated': True/False}
    """
    cfg = await _get_smtp_config(db)
    host = cfg.get('smtp_host', '')
    user = cfg.get('smtp_user', '')
    password = cfg.get('smtp_password', '')
    from_addr = cfg.get('smtp_from', '') or user
    port = int(cfg.get('smtp_port', 587) or 587)
    signature_html = _sanitize_signature((cfg.get('email_signature') or '').strip())

    if not host or not user or not password:
        missing = [k for k, v in [('smtp_host', host), ('smtp_user', user), ('smtp_password', password)] if not v]
        logger.error("SMTP not configured — cannot send email. Missing: %s", ', '.join(missing))
        return {'sent': False, 'simulated': False, 'error': f"SMTP not configured — missing: {', '.join(missing)}"}

    try:
        msg = MIMEMultipart()
        msg['From'] = from_addr
        msg['To'] = to
        msg['Subject'] = subject
        msg['Message-ID'] = make_msgid(domain=(host or 'mail'))
        if reply_to:
            msg['Reply-To'] = reply_to

        body_is_html = body.lstrip().startswith('<')

        if body_is_html:
            plain_body = _strip_html(body)
            body_html = body
        else:
            plain_body = body
            body_html = (
                '<div style="font-family:sans-serif;font-size:14px;white-space:pre-wrap">'
                + body.replace('\n', '<br>\n')
                + '</div>'
            )

        if signature_html:
            body_html += '<br><hr style="border:none;border-top:1px solid #ccc;margin:16px 0">' + signature_html

        if signature_html or body_is_html:
            alt = MIMEMultipart('alternative')
            alt.attach(MIMEText(plain_body, 'plain'))
            alt.attach(MIMEText(body_html, 'html'))
            msg.attach(alt)
        else:
            msg.attach(MIMEText(body, 'plain'))

        if attachments:
            for path in attachments:
                try:
                    with open(path, 'rb') as f:
                        part = MIMEBase('application', 'octet-stream')
                        part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(path)}"')
                    msg.attach(part)
                except Exception as e:
                    logger.error(f"Failed to attach {path}: {e}")

        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(user, password)
            server.sendmail(from_addr, to, msg.as_string())

        message_id = msg.get('Message-ID', f'sent-{subject[:20]}')
        logger.info(f"Email sent to {to}: {subject}")
        return {'sent': True, 'simulated': False, 'message_id': message_id}

    except Exception as e:
        logger.error(f"SMTP send failed: {e}")
        return {'sent': False, 'simulated': False, 'error': str(e)}


async def send_pod_request_to_carrier(
    db: AsyncSession,
    carrier_name: str,
    carrier_email: str,
    delivery_numbers: List[str],
    customer_po: Optional[str] = None,
) -> dict:
    """Send a POD request email to a carrier for missing delivery numbers."""
    # Get template from config
    result = await db.execute(
        select(SystemConfig).where(SystemConfig.key == 'carrier_request_template')
    )
    cfg = result.scalar_one_or_none()
    template = cfg.value if cfg and cfg.value else (
        "Dear {carrier},\n\nPlease provide Proof of Delivery for the following delivery numbers:\n\n"
        "{delivery_list}\n\nKindly reply with the POD documents attached.\n\nThank you."
    )

    delivery_list = '\n'.join(f'  • {dn}' for dn in delivery_numbers)
    po_note = f"\nCustomer PO Reference: {customer_po}" if customer_po else ""
    body = template.format(
        carrier=carrier_name,
        delivery_list=delivery_list + po_note,
    )
    subject = f"POD Request — {len(delivery_numbers)} Delivery Number(s)"
    if customer_po:
        subject += f" — PO {customer_po}"

    return await send_email(db, to=carrier_email, subject=subject, body=body)
