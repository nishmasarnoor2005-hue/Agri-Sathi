import os
import time
import asyncio
import logging
from flask import Flask, request, jsonify, render_template
from livekit import api
from src.config import Config
from src.database import db
from src.utils.resilient_llm import resilient_engine

logger = logging.getLogger("agrisathi.server")
logging.basicConfig(level=logging.INFO)

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates"),
    static_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), "static"),
)

async def _dispatch_agent(room_name: str):
    """Explicitly requests LiveKit to dispatch the agent to the room."""
    try:
        lkapi = api.LiveKitAPI(Config.LIVEKIT_URL, Config.LIVEKIT_API_KEY, Config.LIVEKIT_API_SECRET)
        req = api.CreateAgentDispatchRequest(room=room_name)
        dispatch = await lkapi.agent_dispatch.create_dispatch(req)
        await lkapi.aclose()
        logger.info(f"Agent dispatched to room {room_name} (Dispatch ID: {dispatch.id})")
        return dispatch.id
    except Exception as e:
        logger.warning(f"Agent dispatch notice: {e}")
        return None

async def _delete_room(room_name: str):
    """Forcefully terminates a room session on LiveKit."""
    try:
        lkapi = api.LiveKitAPI(Config.LIVEKIT_URL, Config.LIVEKIT_API_KEY, Config.LIVEKIT_API_SECRET)
        await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
        await lkapi.aclose()
        logger.info(f"Room {room_name} deleted successfully.")
        return True
    except Exception as e:
        logger.error(f"Failed to delete room {room_name}: {e}")
        return False

@app.route("/")
def index():
    return render_template("index.html", livekit_url=Config.LIVEKIT_URL)

@app.route("/api/token", methods=["POST"])
def get_token():
    """Generates a LiveKit JWT token for the web caller and dispatches the agent."""
    data = request.json or {}
    phone = (data.get("phone") or "+919876543210").strip()
    name = (data.get("name") or "Farmer").strip()

    # Normalize phone: ensure only alphanumeric / plus for safe room naming
    safe_phone = "".join(c for c in phone if c.isalnum())
    room_name = f"call-{safe_phone}-{int(time.time())}"

    # Generate Access Token
    token = (
        api.AccessToken(Config.LIVEKIT_API_KEY, Config.LIVEKIT_API_SECRET)
        .with_identity(phone)
        .with_name(name)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
            )
        )
    )
    jwt_token = token.to_jwt()

    # Note: LiveKit Cloud automatically dispatches the registered default agent
    # to newly joined rooms, so explicit dispatch is not needed and would cause duplicate jobs.

    return jsonify({
        "token": jwt_token,
        "url": Config.LIVEKIT_URL,
        "room": room_name,
        "phone": phone,
        "name": name,
    })

@app.route("/api/dashboard/stats", methods=["GET"])
def get_stats():
    """Returns top-level metrics for the dashboard."""
    stats = db.get_stats()
    stats["livekit_url"] = Config.LIVEKIT_URL
    stats["agent_status"] = "online"
    stats["agent_name"] = "agrisathi"
    return jsonify(stats)

@app.route("/api/dashboard/farmers", methods=["GET"])
def get_farmers():
    """Returns list of all registered farmers."""
    farmers = db.get_all_farmers()
    return jsonify(farmers)

@app.route("/api/dashboard/conversations", methods=["GET"])
def get_conversations():
    """Returns recent conversation records with summaries."""
    limit = request.args.get("limit", default=100, type=int)
    conversations = db.get_all_conversations(limit=limit)
    return jsonify(conversations)

@app.route("/api/dashboard/farmer/<phone>", methods=["GET"])
def get_farmer_profile(phone):
    """Returns detailed history for a given farmer."""
    farmer = db.get_farmer(phone)
    if not farmer:
        return jsonify({"error": "Farmer not found"}), 404
    return jsonify(farmer)

@app.route("/api/dashboard/llm-status", methods=["GET"])
def get_llm_status():
    """Returns real-time health and failover metrics for all configured LLM APIs."""
    return jsonify(resilient_engine.get_status_report())

@app.route("/api/hangup", methods=["POST"])
def hangup_room():
    """Allows hanging up a room from the web interface."""
    data = request.json or {}
    room = data.get("room")
    if not room:
        return jsonify({"error": "Missing room parameter"}), 400
    success = asyncio.run(_delete_room(room))
    return jsonify({"success": success})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🌾 AgriSathi Web & Dashboard starting on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
