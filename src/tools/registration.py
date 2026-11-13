# src/tools/registration.py
"""
Farmer Registration Tool
------------------------
Handles saving farmer profile data to the SQLite database.
"""

import logging
from livekit.agents import RunContext, function_tool
from src.database import db

logger = logging.getLogger("agrisathi.tools.registration")


@function_tool()
async def save_caller_name(
    context: RunContext,
    name: str,
):
    """
    Saves the user's name immediately when they tell you their name.
    Call this as soon as the user says their name (e.g., 'My name is Ramesh', 'I am Aman', etc.).
    
    Args:
        name: The full or first name of the caller.
    """
    current_phone = context.session.userdata["phone"]
    logger.info(f"Saving caller name '{name}' for {current_phone}")
    db.save_farmer_name(current_phone, name)
    return f"Saved name: {name}. Address the user as {name} ji and answer their question."


@function_tool()
async def register_farmer(
    context: RunContext,
    name: str,
    place: str = "",
    state: str = "",
    crops: str = "",
    language: str = "hindi",
):
    """
    Registers or updates farmer profile in the database.
    
    Args:
        name: The name of the farmer.
        place: Optional village or city (e.g., Lucknow).
        state: Optional state (e.g., Uttar Pradesh).
        crops: Optional main crops grown (e.g., wheat, rice).
        language: Preferred language. Default is "hindi".
    """
    current_phone = context.session.userdata["phone"]
    logger.info(f"Registering: {name}, {place}, {state}, {crops}, lang={language} for {current_phone}")
    db.register_farmer(current_phone, name, place, state, crops, language)

    # Keep the session's language in sync with what we just saved
    context.session.userdata["language"] = language

    return f"Saved details for {name} ji."


@function_tool()
async def update_language_preference(
    context: RunContext,
    language: str,
):
    """
    Updates the user's preferred language. Call this when the user explicitly
    asks to change the conversation language or says something like 
    "English mein baat karo" or "Hindi mein bolo".
    
    Args:
        language: The new preferred language - "hindi", "english", or "hinglish" etc.
    """
    current_phone = context.session.userdata["phone"]
    logger.info(f"Updating language to {language} for {current_phone}")
    db.update_language(current_phone, language)

    context.session.userdata["language"] = language

    return f"Understood. I will now communicate in {language}."

@function_tool()
async def update_farmer_profile(
    context: RunContext,
    name: str = None,
    place: str = None,
    state: str = None,
    crops: str = None,
):
    """
    Updates specific details in the farmer's profile (Name, Location, Crops).
    Call this when the user wants to correct or update their information.
    Do NOT call this for language changes (use update_language_preference instead).
    
    Args:
        name: New name (if changed).
        place: New village/city (if changed).
        state: New state (if changed).
        crops: New crops list (if changed).
    """
    current_phone = context.session.userdata["phone"]
    logger.info(f"Updating profile for {current_phone}: name={name}, place={place}, state={state}, crops={crops}")
    db.update_farmer_details(current_phone, name=name, place=place, state=state, crops=crops)
    
    return "Profile details updated successfully."
