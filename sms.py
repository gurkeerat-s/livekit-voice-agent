"""SMS helper — sends booking confirmations via Twilio."""

from __future__ import annotations

import logging
import os
import re

import httpx

logger = logging.getLogger("ava.sms")

LISTING_URL = "https://cvre.ca/mls-property/{listing_id}"


def _to_e164(phone: str, default_country: str = "+1") -> str | None:
    """Normalize a phone string to E.164. Returns None if it doesn't look like a phone."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return None
    if len(digits) == 10:
        return f"{default_country}{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if phone.startswith("+"):
        return phone
    return None


def _format_price(price) -> str:
    if not price:
        return "price on request"
    try:
        return f"${int(price):,}"
    except (TypeError, ValueError):
        return str(price)


def build_confirmation(
    *,
    booking_type: str,  # "showing" or "callback"
    caller_name: str,
    listing_address: str | None,
    preferred_time: str | None,
    listings: list[dict],
    brokerage: str,
) -> str:
    """Build a friendly SMS body with booking details + listing links."""
    if booking_type == "showing":
        lines = [f"Hi {caller_name.split()[0]}, thanks for booking with {brokerage}!"]
        if listing_address:
            lines.append(f"Showing: {listing_address}")
        if preferred_time:
            lines.append(f"Time: {preferred_time}")
        lines.append("An agent will confirm the exact slot shortly.")
    else:  # callback
        lines = [
            f"Hi {caller_name.split()[0]}, thanks for reaching out to {brokerage}!",
            "An agent will call you back within 15 minutes during business hours.",
        ]

    if listings:
        lines.append("")
        lines.append("Properties we discussed:")
        # de-dup by listing_id while preserving order
        seen = set()
        for l in listings:
            lid = l.get("listing_id")
            if not lid or lid in seen:
                continue
            seen.add(lid)
            addr = l.get("address", "")
            price = _format_price(l.get("price"))
            url = LISTING_URL.format(listing_id=lid)
            lines.append(f"- {addr} ({price}) {url}")

    return "\n".join(lines)


def send_sms(to: str, body: str) -> bool:
    """Send an SMS via Twilio. Returns True on success."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")

    if not (sid and token and from_number):
        logger.warning("Twilio not configured — skipping SMS to %s", to)
        return False

    to_e164 = _to_e164(to)
    if not to_e164:
        logger.warning("Invalid phone %r — skipping SMS", to)
        return False

    try:
        resp = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, token),
            data={"From": from_number, "To": to_e164, "Body": body},
            timeout=10,
        )
        if resp.status_code >= 400:
            logger.error("Twilio error %s: %s", resp.status_code, resp.text)
            return False
        logger.info("SMS sent to %s", to_e164)
        return True
    except Exception as e:
        logger.error("SMS send failed: %s", e)
        return False
