"""The seam between app/scryfall.py and app/store/.

Only `search()` exists so far - it ranks Scryfall's raw printings so callers
see "most likely match first" instead of every printing Scryfall knows about
(a popular card like Sol Ring has 100+). The read-through cache (`get_card`,
`add_owned`, `refresh_prices`) is added once this is wired to app/store/.
"""
from __future__ import annotations

from rapidfuzz import fuzz

from . import scryfall


def search(text: str, limit: int = 10) -> list[dict]:
    """Return the most likely printings for `text`, best match first.

    Each candidate is scored by name similarity to `text` - this is what
    matters when the fuzzy match itself was uncertain (garbled OCR, a typo).
    When Scryfall's fuzzy match succeeds, every printing shares the same name
    and the score ties; recency breaks the tie, since there's no way to tell
    which specific printing was scanned from a name alone (that's on the user
    to pick from the shortlist).
    """
    candidates = scryfall.search_by_name(text)
    scored = [
        (fuzz.WRatio(text, card.get("name", "")), card.get("released_at") or "", card)
        for card in candidates
    ]
    scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [_to_candidate(card) for _, _, card in scored[:limit]]


def _to_candidate(card: dict) -> dict:
    """Trim a full Scryfall card object down to what a picker UI needs."""
    image = None
    if isinstance(card.get("image_uris"), dict):
        image = card["image_uris"].get("normal")
    return {
        "id": card.get("id"),
        "name": card.get("name"),
        "set_code": card.get("set"),
        "set_name": card.get("set_name"),
        "collector_number": card.get("collector_number"),
        "rarity": card.get("rarity"),
        "released_at": card.get("released_at"),
        "image_uri": image,
        "price_usd": (card.get("prices") or {}).get("usd"),
    }
