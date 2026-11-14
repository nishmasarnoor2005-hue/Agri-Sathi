from dotenv import load_dotenv
load_dotenv()

from livekit.agents import cli, WorkerOptions
import logging
import sys
import time
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
        raise


if __name__ == "__main__":
    print("🚀 AgriSathi Worker starting...")
    while True:
        try:
            cli.run_app(
                WorkerOptions(
                    entrypoint_fnc=entrypoint,
                    prewarm_fnc=prewarm_fnc,
                    initialize_process_timeout=60.0,
                    num_idle_processes=1,
                )
            )
            break
        except SystemExit as se:
            if se.code == 0:
                break
            print(f"⚠️ LiveKit worker exited with code {se.code}. Reconnecting in 5 seconds...")
            time.sleep(5)
        except Exception as e:
            print(f"⚠️ Worker error: {e}. Reconnecting in 5 seconds...")
            time.sleep(5)

