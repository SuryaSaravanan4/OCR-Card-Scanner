"""Turn OCR detections into card candidates, and remember the latest scan.

The Raspberry Pi does the OCR and POSTs its detections to `/api/scan`; this
module is the PC half: parse the footer (set code + collector number) and the
title out of the detections, look the card up through `service.search()`
(cache-aside over Scryfall), and keep the latest result in memory for the
`/scan` page to show. Nothing here touches the OCR model, so it runs in the
normal fast test suite. `scripts/scan.py` shares the same parsing and decision
logic for photos OCR'd locally.

A "box" is EasyOCR's `(bbox, text, confidence)` triple, where `bbox` is four
`[x, y]` corner points.

Footer-first: the footer ("SET · 123/280") identifies exactly one printing, so
it is tried first. The title is read only if the footer is missing,
low-confidence, or matches nothing - then the user picks from a name search.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Callable

from .scryfall import ScryfallOffline

_LEADING_MANA = re.compile(r"^\s*(\{[^}]*\}|\d+)+\s*")
_TRAILING_NOISE = re.compile(r"[\d\{\}].*$")
# Two footer shapes. Older: "123/280". Modern: zero-padded "0123", often with
# the rarity letter glued on by OCR ("R0059"). Bare 1-2 digit boxes are never
# treated as numbers - on the full image those are power/toughness, mana
# symbols, etc. A suffix letter covers variants like "123a".
_NUMBER_WITH_TOTAL = re.compile(r"^\s*(\d{1,4}[a-z★]?)\s*/\s*\d{1,4}\s*$", re.IGNORECASE)
_NUMBER_PADDED = re.compile(r"^\s*[A-Za-z]?\s?(\d{3,4}[a-z★]?)\s*$")
_ALNUM_TOKEN = re.compile(r"[A-Za-z0-9]+")
_NOT_SET_CODES = {"ILLUS", "EN"}
# Below this OCR confidence a footer read counts as "fuzzy" - better to fall
# back to the title than to trust a guess at tiny print.
_MIN_FOOTER_CONF = 0.4


def clean_name(raw: str) -> str:
    """Strip mana-cost clusters and trailing noise OCR tends to glue onto a
    card name, e.g. '{2}{U} Brainstorm' -> 'Brainstorm', 'Sol Ring {1}' ->
    'Sol Ring'."""
    text = _LEADING_MANA.sub("", raw)
    text = _TRAILING_NOISE.sub("", text)
    return text.strip()


def _top(bbox) -> float:
    return min(pt[1] for pt in bbox)


def pick_name_box(
    boxes: list[tuple[list, str, float]], image_height_hint: float | None = None
) -> str | None:
    """Pick the box most likely to be the card's title.

    The title is the topmost real text on the card. On a whole-photo OCR pass
    the card sits somewhere inside the frame, so "top 20% of the image" is
    wrong (a card in a toploader starts well down the photo); instead take the
    topmost box that looks like words - 3+ letters, not a low-confidence
    guess - which skips mana-cost glyphs and set symbols read as stray
    characters. Falls back to the single highest-confidence box if nothing
    qualifies. `image_height_hint` is kept for callers; it is unused now.
    """
    if not boxes:
        return None

    def wordlike(b) -> bool:
        return b[2] >= 0.3 and sum(ch.isalpha() for ch in b[1]) >= 3

    pool = [b for b in boxes if wordlike(b)]
    if pool:
        return min(pool, key=lambda b: _top(b[0]))[1]
    return max(boxes, key=lambda b: b[2])[1]


def _row_tokens(boxes, row_gap: float) -> list[str]:
    """Alphanumeric tokens from `boxes` in reading order. Boxes whose vertical
    centres are within `row_gap` of the previous box share a line, then each
    line is read left to right. (Bucketing by each box's own height instead
    splits one printed line across rows and scrambles the order.)"""
    items = []
    for bbox, text, _ in boxes:
        ys = [pt[1] for pt in bbox]
        items.append(((min(ys) + max(ys)) / 2, min(pt[0] for pt in bbox), text))
    items.sort()

    rows: list[list[tuple[float, str]]] = []
    last_cy = None
    for cy, x, text in items:
        if last_cy is None or cy - last_cy > row_gap:
            rows.append([])
        rows[-1].append((x, text))
        last_cy = cy

    return [tok for row in rows for _, text in sorted(row) for tok in _ALNUM_TOKEN.findall(text)]


def find_set_and_number(boxes: list[tuple[list, str, float]]) -> tuple[str | None, str | None]:
    """Best-effort read of the footer's collector number and set code from
    whole-photo OCR boxes. Low-confidence boxes are ignored (a fuzzy read is
    treated as no read). Returns (None, None) where there is no signal -
    callers should treat that as "fall back to the title", not an error.

    The footer is the lowest number-shaped text on the card, so the number is
    the lowest matching box. The set code is then looked for only in boxes near
    it: the token just before the language code ("PRM" in "PRM • EN"), or -
    for layouts without one - an all-caps 3-5 letter token on the same line.
    """
    confident = [b for b in boxes if b[2] >= _MIN_FOOTER_CONF]

    number_box = collector_number = None
    for box in confident:
        m = _NUMBER_WITH_TOTAL.match(box[1]) or _NUMBER_PADDED.match(box[1])
        if m and (number_box is None or _top(box[0]) > _top(number_box[0])):
            number_box = box
            # Scryfall stores "123", not "0123".
            collector_number = m.group(1).lstrip("0") or "0"
    if number_box is None:
        return None, None

    ys = [pt[1] for pt in number_box[0]]
    height = max(max(ys) - min(ys), 1)
    cy = (min(ys) + max(ys)) / 2
    near = [
        b
        for b in confident
        if b is not number_box and -height <= (_top(b[0]) + height / 2) - cy <= 4 * height
    ]

    tokens = _row_tokens(near, row_gap=height / 2)
    set_code = None
    for i, tok in enumerate(tokens):
        if tok.upper() == "EN" and i > 0 and 3 <= len(tokens[i - 1]) <= 5:
            set_code = tokens[i - 1].lower()
            break
    if set_code is None:
        for b in near:
            for tok in _ALNUM_TOKEN.findall(b[1]):
                if tok.isupper() and 3 <= len(tok) <= 5 and tok not in _NOT_SET_CODES:
                    set_code = tok.lower()
                    break
            if set_code:
                break

    return set_code, collector_number


def boxes_from_detections(detections: list[dict]) -> list[tuple[list, str, float]] | None:
    """Convert the Pi's JSON detections to EasyOCR-style boxes. Returns None
    if any detection lacks a usable bounding box: the footer parse depends on
    box positions, so without them it is skipped rather than guessed at."""
    boxes = []
    for d in detections:
        bbox = d.get("box")
        if not bbox or len(bbox) != 4:
            return None
        try:
            points = [[float(x), float(y)] for x, y in bbox]
            boxes.append((points, str(d["text"]), float(d["confidence"])))
        except (KeyError, TypeError, ValueError):
            return None
    return boxes


def identify(
    boxes: list[tuple[list, str, float]] | None,
    search: Callable[..., list[dict]],
    detections: list[dict] | None = None,
) -> dict:
    """Identify a card from OCR boxes. `search(**params)` does the lookup.

    Returns {"candidates", "exact", "name", "set_code", "collector_number"}.
    exact=True means set code + collector number resolved to exactly one
    printing. When `boxes` is None (the Pi sent no bounding boxes) only the
    title path runs, taking the first word-like detection in reading order.
    """
    set_code = collector_number = None
    if boxes is not None:
        set_code, collector_number = find_set_and_number(boxes)
        if set_code and collector_number:
            hits = search(set_code=set_code, collector_number=collector_number)
            if len(hits) == 1:
                return {
                    "candidates": hits,
                    "exact": True,
                    "name": None,
                    "set_code": set_code,
                    "collector_number": collector_number,
                }

    if boxes is None:
        # No geometry: EasyOCR emits text in roughly top-to-bottom order, so
        # fake a descending y per detection and reuse the title picker.
        boxes = [
            ([[0, i], [1, i], [1, i + 1], [0, i + 1]], d["text"], float(d["confidence"]))
            for i, d in enumerate(detections or [])
        ]
    raw_name = pick_name_box(boxes)
    name = clean_name(raw_name) if raw_name else ""
    if not name:
        return {
            "candidates": [],
            "exact": False,
            "name": None,
            "set_code": set_code,
            "collector_number": collector_number,
        }
    params = {"text": name}
    if set_code:
        # Narrows the name search if the set was right; the number already
        # failed above, so it isn't sent again.
        params["set_code"] = set_code
    return {
        "candidates": search(**params),
        "exact": False,
        "name": name,
        "set_code": set_code,
        "collector_number": collector_number,
    }


_lock = threading.Lock()
_latest: dict | None = None
_counter = 0


def record(detections: list[dict], elapsed_seconds: float | None, search: Callable[..., list[dict]]) -> dict:
    """Identify a scan from the Pi and store it as the latest one."""
    global _latest, _counter
    boxes = boxes_from_detections(detections)
    try:
        result = identify(boxes, search, detections)
        offline = False
    except ScryfallOffline:
        result = {
            "candidates": [],
            "exact": False,
            "name": None,
            "set_code": None,
            "collector_number": None,
        }
        offline = True
    with _lock:
        _counter += 1
        _latest = {
            **result,
            "id": _counter,
            "offline": offline,
            "elapsed_seconds": elapsed_seconds,
            "detections": [{"text": d.get("text"), "confidence": d.get("confidence")} for d in detections],
            "received_at": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        }
        return _latest


def latest() -> dict | None:
    with _lock:
        return _latest


def clear() -> None:
    global _latest, _counter
    with _lock:
        _latest = None
        _counter = 0
