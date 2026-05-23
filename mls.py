"""
MLS search helper — shared between the ava text chatbot and the LiveKit voice agent.

Talks to the Condoville MLS API and returns a clean dict per listing.
"""

from __future__ import annotations

import re

import httpx

MLS_API = "https://cvre.ca/api/mls"
MLS_IMAGE_BASE = "https://cvre.ca"

JUNK_TYPES = {"parking space", "locker", "commercial retail", "industrial"}


def format_listing(item: dict) -> dict:
    photo = item.get("PrimaryPhoto")
    if photo and photo.startswith("/"):
        photo = MLS_IMAGE_BASE + photo

    return {
        "listing_id": item.get("ListingKey"),
        "address": item.get("UnparsedAddress"),
        "price": item.get("ListPrice"),
        "beds": item.get("BedroomsTotal"),
        "baths": item.get("BathroomsTotalInteger"),
        "sqft": item.get("LivingAreaRange"),
        "property_type": (item.get("PropertySubType") or item.get("PropertyType") or "").strip(),
        "city": item.get("City"),
        "neighbourhood": item.get("CityRegion"),
        "description": item.get("PublicRemarks"),
        "transaction_type": item.get("TransactionType"),
        "parking": item.get("ParkingTotal"),
        "tax": item.get("TaxAnnualAmount"),
        "basement": item.get("Basement"),
        "heating": item.get("HeatType"),
        "cooling": item.get("Cooling"),
        "facing": item.get("DirectionFaces"),
        "possession": item.get("PossessionDetails"),
        "brokerage": item.get("ListOfficeName"),
        "photo": photo,
    }


_BAD_CONTAINS = re.compile(r"(\w+)\s+contains\s+('[^']+')", re.IGNORECASE)


def _fix_odata(filter_str: str) -> str:
    """Auto-correct common OData mistakes the LLM makes (`X contains 'Y'`
    -> `contains(X,'Y')`)."""
    return _BAD_CONTAINS.sub(r"contains(\1,\2)", filter_str)


async def search_mls(filter_str: str, orderby: str = "ListPrice desc", top: int = 10) -> list[dict]:
    filter_str = _fix_odata(filter_str)
    params = {
        "filter": filter_str,
        "orderby": orderby,
        "top": str(top),
        "includeMedia": "true",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(MLS_API, params=params)
        data = resp.json()

    return [
        format_listing(item)
        for item in data.get("data", [])
        if item.get("ListPrice", 0) >= 100
        and (item.get("PropertySubType") or "").strip().lower() not in JUNK_TYPES
        and "PARKING" not in (item.get("UnparsedAddress") or "").upper()
    ]


def speakable_price(price: float | int | None) -> str:
    """Turn 850000 into 'eight hundred and fifty thousand dollars' style hint for TTS.

    We keep it loose — the LLM will say it naturally, this just helps when we hand
    raw numbers back from a tool result.
    """
    if not price:
        return "price not listed"
    if price >= 1_000_000:
        millions = price / 1_000_000
        return f"{millions:.2f} million dollars"
    if price >= 1000:
        return f"{int(price / 1000)} thousand dollars"
    return f"{int(price)} dollars per month"
