"""
Ava — LiveKit voice agent for real estate.

Run dev mode (talks via the LiveKit Agents Playground in your browser):
    python agent.py dev

Run in a real LiveKit room (production):
    python agent.py start

Swap providers later by changing the plugin imports / construction below — the
agent shape stays the same.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import xai
from openai.types.realtime.realtime_audio_input_turn_detection import ServerVad
# Pipeline imports kept around in case we revisit ElevenLabs path:
# from livekit.plugins import cerebras, deepgram, elevenlabs, silero

from mls import search_mls, speakable_price
from prompt import SYSTEM_PROMPT, BROKERAGE_NAME
from sms import build_confirmation, send_sms

logger = logging.getLogger("ava")
logger.setLevel(logging.INFO)

BOOKING_URL = os.environ.get("BOOKING_URL", "")


class AvaAgent(Agent):
    """Ava's brain: instructions + tools. Session state (seen listings, caller info)
    lives on the instance so tools can read/write it across the call."""

    def __init__(self, caller_phone: str | None = None) -> None:
        instructions = SYSTEM_PROMPT
        if caller_phone:
            # Last 4 digits — easier for the caller to confirm aloud than reciting all 10.
            last4 = caller_phone[-4:]
            instructions += (
                f"\n\n# Known caller info\n"
                f"The caller's phone number is {caller_phone}. "
                f"When you reach Step 5, do NOT ask for their number. "
                f"Instead say: 'Just to confirm, is the number ending in {last4} the best one to reach you on?' "
                f"If they say yes, pass {caller_phone} as caller_phone to the booking tool. "
                f"If they say no, ask for a different number and use that instead."
            )
        super().__init__(instructions=instructions)
        self.seen_listings: set[str] = set()
        self.caller_name: str | None = None
        self.caller_phone: str | None = caller_phone
        self.caller_email: str | None = None
        self.listings_discussed: list[dict] = []
        # Set in entrypoint() after session.start so end_call can disconnect.
        self.room = None

    @function_tool
    async def search_listings(
        self,
        ctx: RunContext,
        filter: str,
        orderby: str = "ListPrice desc",
    ) -> str:
        """Search the MLS listings database using an OData filter string. You build the filter based on user criteria.

        ALWAYS include these base filters: StandardStatus eq 'Active' and ContractStatus eq 'Available'
        For sales (TransactionType eq 'For Sale'), also add: ListPrice ge 10000
        For rentals (TransactionType eq 'For Lease'), do NOT add ListPrice ge 10000 — rental prices are monthly (e.g. $2500-$5000). For rentals, also add: ListPrice ge 500 (to exclude parking spots and lockers)

        Available fields and operators:
        - ListPrice (number): use ge, le, gt, lt. Use a sensible range based on what the user says. Example: ListPrice ge 500000 and ListPrice le 800000
        - BedroomsTotal (integer): Example: BedroomsTotal ge 3
        - BathroomsTotalInteger (integer): Example: BathroomsTotalInteger ge 2
        - City (string): Use contains() for partial match. MLS cities include area codes like 'Toronto C01', 'Toronto E06' etc. so always use contains. Example: contains(City,'Toronto')
        - CityRegion (string): Neighbourhood name. Example: contains(CityRegion,'Willowdale')
        - TransactionType (string): 'For Sale' or 'For Lease'. Example: TransactionType eq 'For Sale'
        - PropertySubType (string): Values include 'Detached', 'Semi-Detached', 'Condo Apt', 'Att/Row/Twnhouse', 'Condo Townhouse'. Use contains(). Example: contains(PropertySubType,'Detached')
        - LivingAreaRange (string): Text ranges like '< 700', '700-1100', '1000-1199', '1200-1399', '1500-2000', '2000-2500', '2500-3000', '3000-3500', '3500-5000', '5000 +'. Use ne to exclude small ranges.
        - ParkingTotal (integer): Example: ParkingTotal ge 2

        For regional areas, expand to actual city names:
        - GTA = Toronto, Mississauga, Brampton, Vaughan, Markham (use or with contains)
        - York Region = Markham, Vaughan, Richmond Hill, Newmarket, Aurora
        - Peel Region = Brampton, Mississauga, Caledon
        - Durham Region = Oshawa, Whitby, Ajax, Pickering
        - Halton Region = Oakville, Burlington, Milton

        Combine with 'and' / 'or'. Max 5 city contains() clauses per query to avoid API issues.

        Keep track of criteria across the conversation. When the user refines their search, update ALL criteria, not just the new one.

        Args:
            filter: The complete OData $filter string.
            orderby: OData orderby field. Default: ListPrice desc (show best value first). Use 'ListPrice' for lowest first, 'ListPrice desc' for highest first.
        """
        try:
            results = await search_mls(filter, orderby)
        except Exception as e:
            logger.error("MLS search failed: %s", e)
            return json.dumps({"error": "search failed"})

        new_results = [l for l in results if l["listing_id"] not in self.seen_listings]
        if not new_results:
            return json.dumps({"results": [], "message": "no more matches"})

        pick = new_results[0]
        self.seen_listings.add(pick["listing_id"])
        self.listings_discussed.append(pick)

        # Hand the model a TTS-friendly view alongside the raw fields
        return json.dumps({
            "address": pick["address"],
            "price_spoken": speakable_price(pick["price"]),
            "beds": pick["beds"],
            "baths": pick["baths"],
            "sqft": pick["sqft"],
            "property_type": pick["property_type"],
            "neighbourhood": pick["neighbourhood"],
            "city": pick["city"],
            "description": pick["description"],
            "transaction_type": pick["transaction_type"],
            "remaining": len(new_results) - 1,
        })

    @function_tool
    async def book_showing(
        self,
        ctx: RunContext,
        caller_name: str,
        caller_phone: str,
        listing_address: str,
        preferred_time: str,
        caller_email: str | None = None,
    ) -> str:
        """Book a property showing. Call once the caller has confirmed the
        property and given a name, phone, and rough time window.

        Args:
            caller_name: Caller's full name.
            caller_phone: Phone in digits only, e.g. '4165551234'.
            listing_address: Address of the property to show.
            preferred_time: Human-readable preferred date/time, e.g. 'Saturday afternoon'.
            caller_email: Optional email.
        """
        self.caller_name = caller_name
        self.caller_phone = caller_phone
        if caller_email:
            self.caller_email = caller_email

        logger.info(
            "SHOWING REQUEST | %s | %s | %s | %s",
            caller_name, caller_phone, listing_address, preferred_time,
        )

        body = build_confirmation(
            booking_type="showing",
            caller_name=caller_name,
            listing_address=listing_address,
            preferred_time=preferred_time,
            listings=self.listings_discussed,
            brokerage=BROKERAGE_NAME,
        )
        sms_sent = send_sms(caller_phone, body)

        return json.dumps({
            "status": "submitted",
            "sms_sent": sms_sent,
            "confirmation": "an agent will confirm the exact time shortly",
        })

    @function_tool
    async def request_callback(
        self,
        ctx: RunContext,
        caller_name: str,
        caller_phone: str,
        reason: str,
    ) -> str:
        """Request a human agent to call the caller back. Use this when the
        caller wants to talk to an agent, when you can't resolve their question,
        or as a fallback after tool failures.

        Args:
            caller_name: Caller's full name.
            caller_phone: Phone in digits only.
            reason: Brief summary of what they need help with.
        """
        self.caller_name = caller_name
        self.caller_phone = caller_phone
        logger.info("CALLBACK REQUEST | %s | %s | %s", caller_name, caller_phone, reason)

        body = build_confirmation(
            booking_type="callback",
            caller_name=caller_name,
            listing_address=None,
            preferred_time=None,
            listings=self.listings_discussed,
            brokerage=BROKERAGE_NAME,
        )
        sms_sent = send_sms(caller_phone, body)

        return json.dumps({
            "status": "submitted",
            "sms_sent": sms_sent,
            "expected_response": "within fifteen minutes during business hours",
        })

    @function_tool
    async def end_call(self, ctx: RunContext, reason: str) -> str:
        """End the phone call. Use this ONLY when the conversation is naturally complete:
        the caller said goodbye, a booking was confirmed and they have no follow-up,
        or they explicitly asked to hang up.

        Do NOT call this preemptively or because there's a lull. Wait for an
        unambiguous closing signal from the caller.

        Before calling this, say your warm goodbye line FIRST in the same turn,
        then call the tool. Example: "Cool, talk soon — have a good one!" then end_call.

        Args:
            reason: Brief note on why you're ending (e.g. "booking confirmed, caller said bye").
        """
        logger.info("ENDING CALL | %s", reason)
        # Disconnect after a short delay so the final TTS goodbye actually plays
        # before we drop the room.
        async def _delayed():
            await asyncio.sleep(2.0)
            try:
                if self.room is not None:
                    await self.room.disconnect()
            except Exception as e:
                logger.warning("disconnect failed: %s", e)

        asyncio.create_task(_delayed())
        return json.dumps({"status": "ending"})


async def entrypoint(ctx: JobContext) -> None:
    """Called by LiveKit for each new call/room."""
    # Re-load .env here too — LiveKit spawns a separate subprocess per job and
    # module-level load_dotenv() above doesn't always make it in.
    load_dotenv(Path(__file__).parent / ".env")

    await ctx.connect()

    # If this is a phone call (SIP), the participant identity is "sip_+1XXX...".
    # Pull the number out so Ava can confirm it instead of asking blindly.
    caller_phone: str | None = None
    try:
        participant = await ctx.wait_for_participant()
        if participant.identity.startswith("sip_"):
            caller_phone = participant.identity.removeprefix("sip_")
            logger.info("SIP caller detected: %s", caller_phone)
    except Exception as e:
        logger.warning("Could not detect caller phone: %s", e)

    # Grok Voice (think-fast) — realtime speech-to-speech.
    session = AgentSession(
        llm=xai.realtime.RealtimeModel(
            voice="ara",
            turn_detection=ServerVad(
                type="server_vad",
                threshold=0.5,
                prefix_padding_ms=300,
                silence_duration_ms=150,
                create_response=True,
                interrupt_response=True,
            ),
        ),
    )

    agent = AvaAgent(caller_phone=caller_phone)
    agent.room = ctx.room  # so end_call can disconnect
    await session.start(agent=agent, room=ctx.room)

    # Greeting — realtime models don't support session.say(), so prompt the
    # model to speak first via generate_reply.
    await session.generate_reply(
        instructions=(
            f"Greet quickly and casually. ONE sentence, max twelve words. "
            f"Say you're Ava from {BROKERAGE_NAME} and ask how you can help. "
            f"Example: 'Hey, Ava here from {BROKERAGE_NAME} — what can I help with?' "
            "Be snappy, not stiff. No long flowery intro."
        )
    )


if __name__ == "__main__":
    # SWITCHED to SIP forwarder — the original `entrypoint` above (Grok Ava) is
    # kept for easy revert. To restore the local Ava, swap the import below.
    from forwarder import entrypoint as forward_entrypoint
    cli.run_app(WorkerOptions(entrypoint_fnc=forward_entrypoint, agent_name="ava"))
