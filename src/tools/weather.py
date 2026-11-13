# src/tools/weather.py
"""
Live Weather Tool (OpenWeatherMap API)
--------------------------------------
Fetches real-time temperature, weather conditions, humidity, and wind speed
for any city, district, or village in India, and generates practical farming advisories.
"""

import json
import logging
import urllib.parse
import urllib.request
from livekit.agents import RunContext, function_tool
from src.config import Config
from src.database import db

logger = logging.getLogger("agrisathi.tools.weather")


@function_tool()
async def get_live_weather(
    context: RunContext,
    location: str = "",
) -> str:
    """
    Fetches real-time live weather conditions, temperature, humidity, wind, and farming recommendations.
    Call this tool whenever the caller asks about weather, rain, temperature, climate, storm, or forecast.
    
    Args:
        location: City, town, village, or district name (e.g. 'Lucknow', 'Pune', 'Mandya', 'Bhatinda', 'Nagpur').
                  If omitted or empty, will use the caller's registered location.
    """
    api_key = Config.WEATHER_API_KEY
    if not api_key:
        return "Weather service is currently unconfigured."

    # If location is not provided, check user's saved profile or session userdata
    if not location or location.strip() == "":
        phone = context.session.userdata.get("phone", "")
        farmer = db.get_farmer(phone) if phone else None
        if farmer and farmer.get("place"):
            location = farmer["place"]
        else:
            location = "Delhi"

    logger.info(f"Fetching live weather for: {location}")
    encoded_loc = urllib.parse.quote(location.strip())
    url = f"https://api.openweathermap.org/data/2.5/weather?q={encoded_loc}&appid={api_key}&units=metric"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "AgriSathi-VoiceAssistant/2.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())

        city_name = data.get("name", location)
        country = data.get("sys", {}).get("country", "IN")
        weather_list = data.get("weather", [{}])
        condition = weather_list[0].get("description", "clear sky") if weather_list else "clear sky"
        main_weather = weather_list[0].get("main", "").lower()

        main_data = data.get("main", {})
        temp = round(main_data.get("temp", 25.0), 1)
        feels_like = round(main_data.get("feels_like", temp), 1)
        humidity = main_data.get("humidity", 50)

        wind_speed = data.get("wind", {}).get("speed", 0.0)
        wind_kmh = round(wind_speed * 3.6, 1)

        # Practical Farmer Advisory
        advisory = []
        if "rain" in main_weather or "drizzle" in main_weather or "thunderstorm" in condition:
            advisory.append("Rain expected/detected: Avoid pesticide or fertilizer spraying today and ensure drainage.")
        elif humidity > 80:
            advisory.append("High humidity: Inspect crops regularly for fungal infections.")
        elif temp > 35:
            advisory.append("Hot conditions: Ensure timely irrigation to prevent moisture stress.")
        elif temp < 10:
            advisory.append("Chilly conditions: Protect young seedlings and nursery beds from cold injury.")
        else:
            advisory.append("Weather is favorable for standard field operations and routine spraying.")

        advisory_text = " ".join(advisory)

        report = (
            f"Current weather in {city_name} ({country}): {condition}. "
            f"Temperature is {temp}°C (feels like {feels_like}°C), humidity is {humidity}%, "
            f"wind speed is {wind_kmh} km/h. Farmer advisory: {advisory_text}"
        )
        logger.info(f"Weather report for {city_name}: {report}")
        return report

    except urllib.error.HTTPError as e:
        logger.warning(f"Weather API HTTP error for '{location}': {e.code} - {e.reason}")
        if e.code == 404:
            return f"Could not find live weather for '{location}'. Please mention a nearby major district or city."
        return f"Weather service temporarily unavailable (Code {e.code})."
    except Exception as exc:
        logger.error(f"Weather fetch error for '{location}': {exc}")
        return f"Could not retrieve weather for {location} right now: {str(exc)[:60]}"
