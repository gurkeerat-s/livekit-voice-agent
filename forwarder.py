"""
SIP forwarder — when a call lands on the LiveKit-purchased phone number,
immediately bridge it via SIP to ElevenLabs Convai (where the real Ava lives).

The deployed `ava` worker name stays the same; only the entrypoint changes.
"""

from __future__ import annotations

import asyncio
import logging
import os

from livekit import agents, api
from livekit.agents import JobContext, WorkerOptions, cli

logger = logging.getLogger("forwarder")
logger.setLevel(logging.INFO)

# Outbound SIP trunk in LiveKit Cloud that points at sip.rtc.elevenlabs.io
ELEVENLABS_TRUNK_ID = "ST_rV2KTwqL8dKd"
# The phone-number ID in ElevenLabs Convai (becomes the SIP user-part)
ELEVENLABS_DESTINATION = "phnum_3701ks9ahqqkfen82ar83y04m9q5"


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    logger.info("forwarding call from room %s to ElevenLabs", ctx.room.name)

    lkapi = api.LiveKitAPI()
    try:
        await lkapi.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=ELEVENLABS_TRUNK_ID,
                sip_call_to=ELEVENLABS_DESTINATION,
                room_name=ctx.room.name,
                participant_identity="elevenlabs-ava",
                participant_name="Ava",
            )
        )
        logger.info("ElevenLabs participant created — bridge active")
    except Exception as e:
        logger.error("Failed to create SIP participant: %s", e)
        raise
    finally:
        await lkapi.aclose()


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="ava"))
