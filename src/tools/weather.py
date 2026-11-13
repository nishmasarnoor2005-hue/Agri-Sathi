# src/tools/weather.py
"""
Live Agricultural Weather & Soil Tool (AgroMonitoring & OpenWeather API)
------------------------------------------------------------------------
Fetches real-time temperature, weather conditions, humidity, wind, and
underground soil temperature and moisture for any farming location in India.
Uses AgroMonitoring API (5254dacacec80c9ea60335bf302bd46c) with automatic
OpenWeatherMap fallback.
"""

import json
import logging
import urllib.parse
import urllib.request
from typing import Optional, Tuple
from livekit.agents import RunContext, function_tool
from src.config import Config
from src.database import db

logger = logging.getLogger("agrisathi.tools.weather")

# Top Indian agricultural districts and state capitals for zero-latency instant lookup
MAJOR_AGRI_COORDS = {
    "delhi": (28.6139, 77.2090),
    "lucknow": (26.8467, 80.9462),
    "varanasi": (25.3176, 82.9739),
    "kanpur": (26.4499, 80.3319),
    "meerut": (28.9845, 77.7064),
    "agra": (27.1767, 78.0081),
    "bareilly": (28.3670, 79.4304),
    "gorakhpur": (26.7606, 83.3732),
    "chandigarh": (30.7333, 76.7794),
    "ludhiana": (30.9010, 75.8573),
    "amritsar": (31.6340, 74.8723),
    "bhatinda": (30.2110, 74.9455),
    "bathinda": (30.2110, 74.9455),
    "patiala": (30.3398, 76.3869),
    "karnal": (29.6857, 76.9905),
    "hisar": (29.1492, 75.7217),
    "rohtak": (28.8955, 76.6066),
    "jaipur": (26.9124, 75.7873),
    "jodhpur": (26.2389, 73.0243),
    "kota": (25.2138, 75.8648),
    "sri ganganagar": (29.9038, 73.8772),
    "ganganagar": (29.9038, 73.8772),
    "bhopal": (23.2599, 77.4126),
    "indore": (22.7196, 75.8577),
    "ujjain": (23.1765, 75.7885),
    "jabalpur": (23.1815, 79.9864),
    "patna": (25.5941, 85.1376),
    "gaya": (24.7914, 85.0002),
    "muzaffarpur": (26.1209, 85.3647),
    "ranchi": (23.3441, 85.3096),
    "raipur": (21.2514, 81.6296),
    "mumbai": (19.0760, 72.8777),
    "pune": (18.5204, 73.8567),
    "nagpur": (21.1458, 79.0882),
    "nashik": (19.9975, 73.7898),
    "aurangabad": (19.8762, 75.3433),
    "kolhapur": (16.7050, 74.2433),
    "solapur": (17.6599, 75.9064),
    "ahmedabad": (23.0225, 72.5714),
    "surat": (21.1702, 72.8311),
    "rajkot": (22.3039, 70.8022),
    "vadodara": (22.3072, 73.1812),
    "bengaluru": (12.9716, 77.5946),
    "bangalore": (12.9716, 77.5946),
    "mysuru": (12.2958, 76.6394),
    "mysore": (12.2958, 76.6394),
    "mandya": (12.5244, 76.8967),
    "belagavi": (15.8497, 74.4977),
    "dharwad": (15.4589, 75.0078),
    "hubballi": (15.3647, 75.1240),
    "shivamogga": (13.9299, 75.5681),
    "ballari": (15.1394, 76.9214),
    "hyderabad": (17.3850, 78.4867),
    "warangal": (17.9689, 79.5941),
    "karimnagar": (18.4386, 79.1288),
    "vijayawada": (16.5062, 80.6480),
    "guntur": (16.3067, 80.4365),
    "visakhapatnam": (17.6868, 83.2185),
    "tirupati": (13.6288, 79.4192),
    "chennai": (13.0827, 80.2707),
    "coimbatore": (11.0168, 76.9558),
    "madurai": (9.9252, 78.1198),
    "thanjavur": (10.7870, 79.1378),
    "salem": (11.6643, 78.1460),
    "kolkata": (22.5726, 88.3639),
    "bardhaman": (23.2324, 87.8615),
    "bhubaneswar": (20.2961, 85.8245),
    "cuttack": (20.4625, 85.8828),
    "guwahati": (26.1445, 91.7362),
}


def resolve_coordinates(location_name: str) -> Optional[Tuple[float, float, str]]:
    """Resolves coordinates using internal table first, then Nominatim with quick timeout."""
    norm = location_name.strip().lower()
    if norm in MAJOR_AGRI_COORDS:
        lat, lon = MAJOR_AGRI_COORDS[norm]
        return lat, lon, location_name.title()

    # Partial match check in dictionary
    for k, coords in MAJOR_AGRI_COORDS.items():
        if k in norm or norm in k:
            return coords[0], coords[1], location_name.title()

    # Dynamic geocoding fallback
    try:
        encoded = urllib.parse.quote(location_name.strip() + ", India")
        url = f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "AgriSathi-AgroAssistant/2.0"},
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode())
            if data and len(data) > 0:
                lat = float(data[0]["lat"])
                lon = float(data[0]["lon"])
                resolved_name = data[0].get("display_name", location_name).split(",")[0]
                return lat, lon, resolved_name
    except Exception as e:
        logger.debug(f"Geocoding notice for '{location_name}': {e}")

    # Default to Delhi coordinates if unresolvable
    return 28.6139, 77.2090, location_name.title()


@function_tool()
async def get_live_weather(
    context: RunContext,
    location: str = "",
) -> str:
    """
    Fetches real-time live weather conditions, temperature, humidity, wind, and
    underground soil temperature and moisture data for Indian farming areas.
    Call this tool whenever the caller asks about weather, rain, temperature,
    climate, soil conditions, or crop spraying suitability.

    Args:
        location: City, town, village, or district name (e.g. 'Lucknow', 'Pune', 'Mandya', 'Bathinda', 'Nagpur').
                  If omitted or empty, uses the caller's registered location.
    """
    agro_key = Config.AGRO_API_KEY or "5254dacacec80c9ea60335bf302bd46c"
    backup_key = Config.WEATHER_API_KEY or "30e1bb5017240d0ab32be68eea525ba1"

    # Default location from profile if blank
    if not location or location.strip() == "":
        phone = context.session.userdata.get("phone", "")
        farmer = db.get_farmer(phone) if phone else None
        if farmer and farmer.get("place"):
            location = farmer["place"]
        else:
            location = "Delhi"

    loc_clean = location.strip()
    logger.info(f"Fetching agricultural weather & soil data for: {loc_clean}")

    # 1. Primary: Specialized AgroMonitoring API (Weather + Soil Moisture & Temp)
    coords_res = resolve_coordinates(loc_clean)
    if coords_res:
        lat, lon, display_city = coords_res
        try:
            url_weather = f"https://api.agromonitoring.com/agro/1.0/weather?lat={lat}&lon={lon}&appid={agro_key}"
            url_soil = f"https://api.agromonitoring.com/agro/1.0/soil?lat={lat}&lon={lon}&appid={agro_key}"

            req_w = urllib.request.Request(url_weather, headers={"User-Agent": "AgriSathi-App/2.0"})
            with urllib.request.urlopen(req_w, timeout=4) as rw:
                data_w = json.loads(rw.read().decode())

            soil_info = ""
            try:
                req_s = urllib.request.Request(url_soil, headers={"User-Agent": "AgriSathi-App/2.0"})
                with urllib.request.urlopen(req_s, timeout=4) as rs:
                    data_s = json.loads(rs.read().decode())

                t10_c = round(data_s.get("t10", 298.15) - 273.15, 1)
                moisture_pct = round(data_s.get("moisture", 0.15) * 100, 1)

                if moisture_pct < 12.0:
                    moisture_state = f"{moisture_pct}% (Dry soil: Irrigation recommended soon)"
                elif moisture_pct > 32.0:
                    moisture_state = f"{moisture_pct}% (High soil moisture: Delay watering and check drainage)"
                else:
                    moisture_state = f"{moisture_pct}% (Optimal soil moisture for crop growth)"

                soil_info = f" Soil temperature at 10cm depth is {t10_c}°C, and soil moisture is {moisture_state}."
            except Exception as se:
                logger.debug(f"Soil sub-query notice: {se}")

            # Weather metrics
            temp_c = round(data_w.get("main", {}).get("temp", 298.15) - 273.15, 1)
            feels_c = round(data_w.get("main", {}).get("feels_like", data_w.get("main", {}).get("temp", 298.15)) - 273.15, 1)
            humidity = data_w.get("main", {}).get("humidity", 50)
            wind_speed = round(data_w.get("wind", {}).get("speed", 0.0) * 3.6, 1)

            weather_list = data_w.get("weather", [{}])
            condition = weather_list[0].get("description", "clear sky") if weather_list else "clear sky"
            main_weather = weather_list[0].get("main", "").lower()

            # Farming advisory
            advisory = []
            if "rain" in main_weather or "drizzle" in main_weather or "thunderstorm" in condition:
                advisory.append("Rain detected/forecasted: Hold off on pesticide or foliar fertilizer spraying today.")
            elif wind_speed > 20:
                advisory.append("High winds: Avoid spraying chemicals to prevent spray drift.")
            elif temp_c > 35:
                advisory.append("High daytime heat: Conduct irrigation during early morning or evening hours.")
            elif temp_c < 12:
                advisory.append("Chilly temperature: Protect young nurseries and vegetable beds from cold stress.")
            else:
                advisory.append("Weather is favorable for standard field operations and routine spraying.")

            advisory_text = " ".join(advisory)
            report = (
                f"Current live weather in {display_city}: {condition}. "
                f"Air temperature is {temp_c}°C (feels like {feels_c}°C), humidity is {humidity}%, "
                f"wind speed is {wind_speed} km/h.{soil_info} "
                f"Farmer advisory: {advisory_text}"
            )
            logger.info(f"AgroMonitoring report for {display_city}: {report}")
            return report

        except Exception as agro_err:
            logger.warning(f"AgroMonitoring error for '{loc_clean}': {agro_err}. Falling back to OpenWeatherMap...")

    # 2. Fallback: OpenWeatherMap standard weather endpoint
    try:
        encoded_loc = urllib.parse.quote(loc_clean)
        url_fb = f"https://api.openweathermap.org/data/2.5/weather?q={encoded_loc}&appid={backup_key}&units=metric"
        req_fb = urllib.request.Request(url_fb, headers={"User-Agent": "AgriSathi-App/2.0"})
        with urllib.request.urlopen(req_fb, timeout=4) as rfb:
            data_fb = json.loads(rfb.read().decode())

        city_name = data_fb.get("name", loc_clean)
        weather_list = data_fb.get("weather", [{}])
        cond_fb = weather_list[0].get("description", "clear sky") if weather_list else "clear sky"
        main_w_fb = weather_list[0].get("main", "").lower()
        temp_fb = round(data_fb.get("main", {}).get("temp", 25.0), 1)
        hum_fb = data_fb.get("main", {}).get("humidity", 50)
        wind_fb = round(data_fb.get("wind", {}).get("speed", 0.0) * 3.6, 1)

        advisory_fb = "Weather is favorable for routine farming."
        if "rain" in main_w_fb or "drizzle" in main_w_fb:
            advisory_fb = "Rain expected: Avoid chemical spraying today and ensure proper field drainage."
        elif temp_fb > 35:
            advisory_fb = "High heat: Ensure timely crop irrigation."

        return (
            f"Current weather in {city_name}: {cond_fb}. "
            f"Temperature is {temp_fb}°C, humidity is {hum_fb}%, wind is {wind_fb} km/h. "
            f"Advisory: {advisory_fb}"
        )
    except Exception as fb_err:
        logger.error(f"Weather fallback error for '{loc_clean}': {fb_err}")
        return f"Could not retrieve live weather for {loc_clean} right now. Please mention a nearby major district or city."
