from livekit.agents import cli, WorkerOptions
import logging
import sys
from src.agent import entrypoint
from src.config import Config

# Configure basic logging for the application
logging.basicConfig(level=logging.INFO)


def prewarm_fnc(proc):
    """
    Called before the worker starts accepting jobs.
    We validate config here and prewarm VAD to prevent in-call event loop stalls.
    """
    try:
        Config.validate()
        print("✅ Configuration validated successfully!")
        from livekit.plugins import silero
        proc.userdata["vad"] = silero.VAD.load()
        print("✅ Silero VAD prewarmed successfully!")
    except Exception as e:
        print(f"❌ Initialization Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    print("🚀 AgriSathi Worker starting...")
    # Start the worker
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm_fnc,  # Validate config at runtime, not build time
        )
    )

