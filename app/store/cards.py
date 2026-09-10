"""The `cards` table: one row per printing, static data, effectively write-once.

`upsert_card` takes a Scryfall card object so the Phase 3 service can pass an API
response straight through.
"""
from __future__ import annotations

import json

from ..db import connect
from ._common import utcnow


def _row_from_scryfall(data: dict) -> dict:
    """Map a Scryfall card object onto our `cards` columns."""
    image = None
    if isinstance(data.get("image_uris"), dict):
        uris = data["image_uris"]
        image = uris.get("normal") or uris.get("large") or uris.get("small")
    elif isinstance(data.get("card_faces"), list) and data["card_faces"]:
        face = data["card_faces"][0]
        if isinstance(face.get("image_uris"), dict):
            image = face["image_uris"].get("normal")

    return {
        "id": data["id"],
        "oracle_id": data.get("oracle_id"),
        "name": data.get("name"),
        "set_code": data.get("set"),
        "set_name": data.get("set_name"),
        "collector_number": data.get("collector_number"),
        "rarity": data.get("rarity"),
        "type_line": data.get("type_line"),
        "mana_cost": data.get("mana_cost"),
        "oracle_text": data.get("oracle_text"),
        "image_uri": image,
        "lang": data.get("lang", "en"),
        "released_at": data.get("released_at"),
        "raw_json": json.dumps(data, separators=(",", ":")),
        "fetched_at": utcnow(),
    }


def upsert_card(data: dict) -> None:
    """Insert a card, or refresh its static columns if it is already cached.

    `fetched_at` keeps its original value on update so it records first-seen time.
    """
    row = _row_from_scryfall(data)
    cols = list(row.keys())
    placeholders = ",".join(f":{c}" for c in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("id", "fetched_at"))
    sql = (
        f"INSERT INTO cards ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}"
    )
    with connect() as conn:
        conn.execute(sql, row)


def get_card(card_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    return dict(row) if row else None
