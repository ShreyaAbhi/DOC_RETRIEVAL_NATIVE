"""
power_automate_service.py
HTTP client for triggering Power Automate cloud flow webhooks.

The chain is:
  POD System  →  Cloud Flow (HTTP trigger)  →  Desktop Flow

Both webhook URLs are stored in system_config and are optional.
If a URL is blank/null the corresponding trigger is silently skipped.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30  # seconds — cloud flows can take a moment to accept


async def trigger_webhook(url: str, payload: dict) -> bool:
    """POST JSON payload to a Power Automate HTTP-trigger webhook URL.
    Returns True on 2xx, False on any error (never raises).
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        logger.info("Power Automate webhook OK [%s] → HTTP %s", url[:80], resp.status_code)
        return True
    except httpx.HTTPStatusError as e:
        logger.error("Power Automate webhook HTTP error [%s]: %s", url[:80], e)
    except httpx.RequestError as e:
        logger.error("Power Automate webhook connection error [%s]: %s", url[:80], e)
    except Exception as e:
        logger.error("Power Automate webhook unexpected error [%s]: %s", url[:80], e)
    return False


async def trigger_for_order(
    order,
    packing_slip_url: Optional[str],
    invoice_url: Optional[str],
) -> dict:
    """Fire configured webhooks for a single order.

    Sends a JSON payload with order identifiers and the document_type so the
    receiving cloud/desktop flow knows what to retrieve.

    Returns a dict with keys 'packing_slip' and/or 'invoice' mapped to bool
    success flags (only present if the corresponding URL was configured).
    """
    now = datetime.now(timezone.utc).isoformat()
    base_payload = {
        "order_id":              str(order.id),
        "customer_order_number": order.customer_order_number,
        "my_delivery_number":    order.my_delivery_number,
        "invoice_number":        order.invoice_number,
        "customer_name":         order.customer_name,
        "triggered_at":          now,
    }

    results = {}
    if packing_slip_url:
        results["packing_slip"] = await trigger_webhook(
            packing_slip_url,
            {**base_payload, "document_type": "packing_slip"},
        )
    if invoice_url:
        results["invoice"] = await trigger_webhook(
            invoice_url,
            {**base_payload, "document_type": "invoice"},
        )
    return results
