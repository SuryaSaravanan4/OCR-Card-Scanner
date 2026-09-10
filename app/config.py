"""Central configuration knobs.

Phase 1 only needs the paths and the price TTL. The Scryfall settings are here
now so the client added in Phase 2 has one place to read from.
"""
from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "cards.db"

# Refetch a card's price if the stored copy is older than this many days.
PRICE_TTL_DAYS = 7

# --- Scryfall (used from Phase 2 onward; unused in Phase 1) ---
SCRYFALL_BASE = "https://api.scryfall.com"
# Scryfall asks every client to send an identifying User-Agent with a contact.
USER_AGENT = "OCR-Card-Scanner/0.1 (contact: you@example.com)"
# Minimum seconds between Scryfall requests (their guidance is 50-100ms).
SCRYFALL_MIN_INTERVAL = 0.1
