# src/tools/__init__.py
"""
AgriSathi Tools Package
-----------------------
This package contains all the tools (functions) that the AI agent can call.
Each tool is defined in its own file for modularity.
"""

from src.tools.registration import save_caller_name, register_farmer, update_language_preference, update_farmer_profile
from src.tools.web_search import web_search
from src.tools.language_detection import detect_language
from src.tools.weather import get_live_weather

# Export all tools as a list for easy import into the Agent
ALL_TOOLS = [save_caller_name, register_farmer, update_language_preference, update_farmer_profile, web_search, detect_language, get_live_weather]
