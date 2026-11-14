import sqlite3
import logging
import os
from datetime import datetime
from typing import Optional, List, Dict

logger = logging.getLogger("agrisathi.db")

class FarmerDB:
    """
    The 'Memory Bank' of AgriSathi.
    Handles persistent storage of farmer profiles and conversation history.
    """
    
    def __init__(self, db_path: str = "data/agrisathi.db"):
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        """Initializes the database schema"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Table 1: Farmer Profiles
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS farmers (
                    phone TEXT PRIMARY KEY,
                    name TEXT,
                    place TEXT,
                    state TEXT,
                    crops TEXT,
                    latitude REAL,
                    longitude REAL,
                    preferred_language TEXT DEFAULT 'hindi',
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Migration check: Ensure latitude & longitude columns exist in older database files
            cursor.execute("PRAGMA table_info(farmers)")
            cols = [row[1] for row in cursor.fetchall()]
            if "latitude" not in cols:
                cursor.execute("ALTER TABLE farmers ADD COLUMN latitude REAL")
            if "longitude" not in cols:
                cursor.execute("ALTER TABLE farmers ADD COLUMN longitude REAL")
            
            # Table 2: Conversations
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT,
                    summary TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (phone) REFERENCES farmers (phone)
                )
            ''')
            
            # Migrations are handled by schema above for new setups
            conn.commit()
            logger.info("Database initialized successfully")

    def get_farmer(self, phone: str) -> Optional[Dict]:
        """Fetches a farmer's profile and their last 5 conversation summaries"""
        with self._get_connection() as conn:
            # Normally, SQLite returns data as lists (like [name, village, ...]). By setting this, we get data as dictionaries (like {name: "Ramesh", village: ...}).
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("SELECT * FROM farmers WHERE phone = ?", (phone,))
            farmer = cursor.fetchone()
            
            if not farmer:
                return None
            
            cursor.execute('''
                SELECT summary FROM conversations 
                WHERE phone = ? 
                ORDER BY timestamp DESC, id DESC LIMIT 5
            ''', (phone,))
            summaries = [row['summary'] for row in cursor.fetchall()]
            
            return {
                "name": farmer['name'],
                "place": farmer['place'],
                "state": farmer['state'],
                "latitude": farmer['latitude'] if 'latitude' in farmer.keys() else None,
                "longitude": farmer['longitude'] if 'longitude' in farmer.keys() else None,
                "crops": farmer['crops'],
                "preferred_language": farmer['preferred_language'] or 'hindi',
                "history": summaries
            }

    def register_farmer(self, phone: str, name: str, place: str = "", state: str = "", crops: str = "", language: str = "hindi"):
        """Creates or updates a farmer's profile"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO farmers (phone, name, place, state, crops, preferred_language)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (phone, name, place, state, crops, language))
            conn.commit()
            logger.info(f"Farmer {name} ({phone}) registered/updated in {place}, {state}")

    def update_farmer_location(self, phone: str, place: str, state: str = "", lat: float = None, lon: float = None):
        """Stores or updates the farmer's registered location and coordinates."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT phone, name FROM farmers WHERE phone = ?", (phone,))
            row = cursor.fetchone()
            if row:
                cursor.execute('''
                    UPDATE farmers
                    SET place = ?, state = ?, latitude = ?, longitude = ?
                    WHERE phone = ?
                ''', (place, state, lat, lon, phone))
            else:
                cursor.execute('''
                    INSERT INTO farmers (phone, name, place, state, latitude, longitude, preferred_language)
                    VALUES (?, '', ?, ?, ?, ?, 'hindi')
                ''', (phone, place, state, lat, lon))
            conn.commit()
            logger.info(f"Updated registered location for {phone}: {place}, {state} (lat: {lat}, lon: {lon})")

    def save_farmer_name(self, phone: str, name: str):
        """Saves or updates just the farmer's name, inserting a row if new."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT phone FROM farmers WHERE phone = ?", (phone,))
            row = cursor.fetchone()
            if row:
                cursor.execute("UPDATE farmers SET name = ? WHERE phone = ?", (name, phone))
            else:
                cursor.execute(
                    "INSERT INTO farmers (phone, name, place, state, crops, preferred_language) VALUES (?, ?, '', '', '', 'hindi')",
                    (phone, name)
                )
            conn.commit()
            logger.info(f"Saved farmer name: '{name}' for {phone}")

    def update_language(self, phone: str, language: str):
        """Updates ONLY the preferred language - can be called mid-conversation"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE farmers SET preferred_language = ? WHERE phone = ?
            ''', (language, phone))
            conn.commit()
            logger.info(f"Language updated to {language} for {phone}")

    def update_farmer_details(self, phone: str, name: str = None, place: str = None, state: str = None, crops: str = None):
        """Updates specific fields of a farmer's profile"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Dynamic query building
            updates = []
            params = []
            
            if name:
                updates.append("name = ?")
                params.append(name)
            if place:
                updates.append("place = ?")
                params.append(place)
            if state:
                updates.append("state = ?")
                params.append(state)
            if crops:
                updates.append("crops = ?")
                params.append(crops)
                
            if not updates:
                return # Nothing to update
                
            params.append(phone)
            query = f"UPDATE farmers SET {', '.join(updates)} WHERE phone = ?"
            
            cursor.execute(query, tuple(params))
            if cursor.rowcount == 0:
                cursor.execute(
                    "INSERT INTO farmers (phone, name, place, state, crops, preferred_language) VALUES (?, ?, ?, ?, ?, 'hindi')",
                    (phone, name or '', place or '', state or '', crops or '')
                )
            conn.commit()
            logger.info(f"Updated profile for {phone}: {updates}")

    def add_summary(self, phone: str, summary: str):
        """
        Records a new conversation summary.
        Enforces a limit of 5 conversations per farmer - oldest gets deleted.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # 1. Insert the new summary first
            cursor.execute('''
                INSERT INTO conversations (phone, summary)
                VALUES (?, ?)
            ''', (phone, summary))
            
            # 2. Enforce the 5-conversation limit by deleting oldest entries
            # We keep only the 5 most recent by timestamp
            cursor.execute('''
                DELETE FROM conversations 
                WHERE phone = ? AND id NOT IN (
                    SELECT id FROM conversations 
                    WHERE phone = ? 
                    ORDER BY timestamp DESC, id DESC 
                    LIMIT 5
                )
            ''', (phone, phone))
            
            conn.commit()
            logger.info(f"Summary added for {phone} (5-message limit enforced)")

    def get_all_farmers(self) -> List[Dict]:
        """Fetches all registered farmers"""
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM farmers ORDER BY registered_at DESC")
            return [dict(row) for row in cursor.fetchall()]

    def get_all_conversations(self, limit: int = 100) -> List[Dict]:
        """Fetches recent conversations with farmer details joined"""
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT c.id, c.phone, c.summary, c.timestamp,
                       f.name, f.place, f.state, f.crops, f.preferred_language
                FROM conversations c
                LEFT JOIN farmers f ON c.phone = f.phone
                ORDER BY c.timestamp DESC, c.id DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_stats(self) -> Dict:
        """Returns statistics for dashboard metrics"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM farmers")
            total_farmers = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM conversations")
            total_conversations = cursor.fetchone()[0]

            cursor.execute("""
                SELECT preferred_language, COUNT(*) as cnt
                FROM farmers
                WHERE preferred_language IS NOT NULL AND preferred_language != ''
                GROUP BY preferred_language
                ORDER BY cnt DESC
            """)
            languages = {row[0]: row[1] for row in cursor.fetchall()}

            return {
                "total_farmers": total_farmers,
                "total_conversations": total_conversations,
                "languages": languages,
            }



# Singleton instance
db = FarmerDB()
