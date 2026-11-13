# src/agent.py
"""
AgriSathi AI - Main Agent Orchestrator
--------------------------------------
Handles LiveKit lifecycle, STT/TTS pipeline, and session coordination.
"""
import logging
import asyncio
from livekit.agents import Agent, AgentSession, AutoSubscribe, JobContext
from livekit.plugins import google
from livekit.plugins import silero
from livekit.plugins import openai as lk_openai
from livekit.plugins.deepgram import STT as DeepgramSTT
from livekit.plugins.cartesia import TTS as CartesiaTTS
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from src.config import Config
from src.database import db
from src.tools import ALL_TOOLS
from src.prompt import SYSTEM_PROMPT

# Modular Handlers
from src.handlers.summary import save_session_summary
from src.handlers.conversation import attach_conversation_tracker
from src.utils.telephony import hangup_call
from src.utils.resilient_llm import resilient_engine

logger = logging.getLogger("agrisathi.agent")
logger.setLevel(logging.INFO)

class AgriSathiAssistant(Agent):
    """AgriSathi Identity and Tool definitions."""
    def __init__(self, instructions: str) -> None:
        super().__init__(
            instructions=instructions,
            tools=ALL_TOOLS,
        )

    @property
    def instructions(self) -> str:
        return self._instructions

    @instructions.setter
    def instructions(self, value: str) -> None:
        self._instructions = value

async def entrypoint(ctx: JobContext):
    """Main call handler."""
    logger.info(f"Incoming call in room: {ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Use prewarmed VAD instance to prevent event loop stalls
    vad_instance = getattr(ctx.proc, "userdata", {}).get("vad") if hasattr(ctx, "proc") else None
    if vad_instance is None:
        vad_instance = silero.VAD.load()

    # 1. Session Setup with Zero-Downtime Auto-Switch LLM
    session = AgentSession(
        stt=DeepgramSTT(model="nova-3", language='multi'),
        llm=resilient_engine,
        tts=CartesiaTTS(model="sonic-3", voice="faf0731e-dfb9-4cfc-8119-259a79b27e12"),
        vad=vad_instance,
    )

    # Temporary default userdata while participant connects
    session.userdata = {"phone": "caller", "language": "english"}

    # 2. Attach Modular Handlers
    conversation_log = []
    attach_conversation_tracker(session, conversation_log)

    # 3. Start Agent IMMEDIATELY so RoomIO is established within the 10s deadline
    assistant = AgriSathiAssistant(instructions=SYSTEM_PROMPT)
    await session.start(
        room=ctx.room,
        agent=assistant,
    )
    logger.info(f"Agent session started in room: {ctx.room.name}")

    # 4. Identity & Profile (Now safe to await participant; RoomIO is already alive)
    try:
        participant = await ctx.wait_for_participant()
        phone = participant.identity or "caller"
    except Exception as e:
        logger.info(f"Participant wait notice in {ctx.room.name}: {e}")
        phone = "caller"

    farmer_profile = db.get_farmer(phone)
    session_language = "english"
    if farmer_profile:
        history = "\n- ".join(farmer_profile['history'][-5:]) if farmer_profile['history'] else "No history."
        lang = farmer_profile.get('preferred_language', 'English')
        session_language = lang.lower() if lang else "english"
        assistant.instructions = SYSTEM_PROMPT + f"\n\n## User: {farmer_profile['name']}\n- Location: {farmer_profile['place']}\n- Past: {history}\n- Language: {lang}"
    else:
        assistant.instructions = SYSTEM_PROMPT + "\n\n## Status: New User. Greet in English/Neutral and detect language."

    session.userdata["phone"] = phone
    session.userdata["language"] = session_language

    # Immediately greet caller: ask name
    if farmer_profile and farmer_profile.get('name'):
        greeting_text = f"Hello {farmer_profile['name']} ji! Welcome back to AgriSathi. How can I help you today?"
    else:
        greeting_text = "Hello! I am AgriSathi. May I know your name?"

    session.say(greeting_text, allow_interruptions=True)

    # 5. Call Duration Watchdog (Hangs up entire telephony call)
    async def _enforce_time_limit():
        try:
            await asyncio.sleep(Config.MAX_CALL_DURATION)
            logger.warning(f"Timeout ({Config.MAX_CALL_DURATION}s) reached. Hanging up.")
            await hangup_call(ctx.room.name)
        except asyncio.CancelledError:
            pass

    timer_task = asyncio.create_task(_enforce_time_limit())

    # 6. Cleanup & Summarization
    async def cleanup():
        timer_task.cancel()
        current_phone = session.userdata.get("phone", phone)
        await save_session_summary(current_phone, conversation_log)
        logger.info(f"Session cleaned up for {current_phone}")

    ctx.add_shutdown_callback(cleanup)
