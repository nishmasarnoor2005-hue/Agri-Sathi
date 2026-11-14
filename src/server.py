import os
import time
import math
import json
import urllib.request
import asyncio
import logging
from flask import Flask, request, jsonify, render_template
from livekit import api
from src.config import Config
from src.database import db
from src.utils.resilient_llm import resilient_engine

logger = logging.getLogger("agrisathi.server")
logging.basicConfig(level=logging.INFO)

STATE_BY_CITY = {
    "delhi": "Delhi", "lucknow": "Uttar Pradesh", "varanasi": "Uttar Pradesh",
    "kanpur": "Uttar Pradesh", "meerut": "Uttar Pradesh", "agra": "Uttar Pradesh",
    "bareilly": "Uttar Pradesh", "gorakhpur": "Uttar Pradesh", "chandigarh": "Punjab",
    "ludhiana": "Punjab", "amritsar": "Punjab", "bhatinda": "Punjab", "bathinda": "Punjab",
    "patiala": "Punjab", "karnal": "Haryana", "hisar": "Haryana", "rohtak": "Haryana",
    "jaipur": "Rajasthan", "jodhpur": "Rajasthan", "kota": "Rajasthan",
    "sri ganganagar": "Rajasthan", "ganganagar": "Rajasthan", "bhopal": "Madhya Pradesh",
    "indore": "Madhya Pradesh", "ujjain": "Madhya Pradesh", "jabalpur": "Madhya Pradesh",
    "patna": "Bihar", "gaya": "Bihar", "muzaffarpur": "Bihar", "ranchi": "Jharkhand",
    "raipur": "Chhattisgarh", "mumbai": "Maharashtra", "pune": "Maharashtra",
    "nagpur": "Maharashtra", "nashik": "Maharashtra", "aurangabad": "Maharashtra",
    "kolhapur": "Maharashtra", "solapur": "Maharashtra", "ahmedabad": "Gujarat",
    "surat": "Gujarat", "rajkot": "Gujarat", "vadodara": "Gujarat",
    "bengaluru": "Karnataka", "bangalore": "Karnataka", "mysuru": "Karnataka",
    "mysore": "Karnataka", "mandya": "Karnataka", "belagavi": "Karnataka",
    "dharwad": "Karnataka", "hubballi": "Karnataka", "shivamogga": "Karnataka",
    "ballari": "Karnataka", "hyderabad": "Telangana", "warangal": "Telangana",
    "karimnagar": "Telangana", "vijayawada": "Andhra Pradesh", "guntur": "Andhra Pradesh",
    "visakhapatnam": "Andhra Pradesh", "tirupati": "Andhra Pradesh", "chennai": "Tamil Nadu",
    "coimbatore": "Tamil Nadu", "madurai": "Tamil Nadu", "thanjavur": "Tamil Nadu",
    "salem": "Tamil Nadu", "kolkata": "West Bengal", "bardhaman": "West Bengal",
    "bhubaneswar": "Odisha", "cuttack": "Odisha", "guwahati": "Assam",
}

def reverse_geocode_coordinates(lat: float, lon: float):
    """
    Resolves (lat, lon) to a city/district and state.
    Uses OpenStreetMap Nominatim reverse geocoding,
    falling back to nearest known Indian agricultural center.
    """
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=12"
        req = urllib.request.Request(url, headers={"User-Agent": "AgriSathi-LocationService/2.0"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode())
            addr = data.get("address", {})
            place = (
                addr.get("city")
                or addr.get("town")
                or addr.get("district")
                or addr.get("state_district")
                or addr.get("county")
                or addr.get("village")
                or addr.get("suburb")
                or ""
            )
            state = addr.get("state", "")
            if place:
                return place, state
    except Exception as e:
        logger.debug(f"Nominatim reverse geocode notice: {e}")

    # Fallback to closest known agricultural district
    from src.tools.weather import MAJOR_AGRI_COORDS
    best_dist = float("inf")
    best_city = "lucknow"
    for city, (clat, clon) in MAJOR_AGRI_COORDS.items():
        d = math.hypot(lat - clat, lon - clon)
        if d < best_dist:
            best_dist = d
            best_city = city

    city_title = best_city.title()
    state = STATE_BY_CITY.get(best_city.lower(), "India")
    return city_title, state

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
    raw_name = (data.get("name") or "").strip()
    name = raw_name if raw_name and raw_name.lower() != "new farmer" else ""

    # If the user explicitly provided a name on frontend, save it to the database
    if name:
        db.save_farmer_name(phone, name)

    # Normalize phone: ensure only alphanumeric / plus for safe room naming
    safe_phone = "".join(c for c in phone if c.isalnum())
    room_name = f"call-{safe_phone}-{int(time.time())}"

    # Generate Access Token
    token = (
        api.AccessToken(Config.LIVEKIT_API_KEY, Config.LIVEKIT_API_SECRET)
        .with_identity(phone)
        .with_name(name or "Farmer")
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

    farmer = db.get_farmer(phone) or {}

    return jsonify({
        "token": jwt_token,
        "url": Config.LIVEKIT_URL,
        "room": room_name,
        "phone": phone,
        "name": name,
        "place": farmer.get("place", ""),
        "state": farmer.get("state", ""),
    })

@app.route("/api/farmer/location", methods=["POST"])
def update_farmer_location():
    """Receives browser geolocation coordinates, reverse-geocodes, and registers farmer location."""
    data = request.json or {}
    phone = (data.get("phone") or "").strip()
    try:
        lat = float(data.get("latitude"))
        lon = float(data.get("longitude"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid or missing latitude/longitude coordinates"}), 400

    if not phone:
        return jsonify({"error": "Missing farmer phone identifier"}), 400

    place, state = reverse_geocode_coordinates(lat, lon)
    db.update_farmer_location(phone, place=place, state=state, lat=lat, lon=lon)
    logger.info(f"Registered location for {phone}: {place}, {state} ({lat:.4f}, {lon:.4f})")

    return jsonify({
        "success": True,
        "phone": phone,
        "place": place,
        "state": state,
        "latitude": lat,
        "longitude": lon,
    })

@app.route("/api/farmer/name", methods=["POST"])
def update_farmer_name():
    """Saves farmer name from browser input."""
    data = request.json or {}
    phone = (data.get("phone") or "").strip()
    name = (data.get("name") or "").strip()
    if not phone or not name:
        return jsonify({"error": "Missing phone or name"}), 400

    db.save_farmer_name(phone, name)
    return jsonify({"success": True, "phone": phone, "name": name})

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
