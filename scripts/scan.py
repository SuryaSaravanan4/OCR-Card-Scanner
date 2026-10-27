"""OCR a photo of a Magic card and look it up against this app's own API,
instead of hitting Scryfall directly - keeps OCR fully decoupled from the
cache-aside layer and lets scans work even when this machine is offline (the
API itself falls back to cached data; see app/service.py's offline handling).

Run the web app first:

    python -m uvicorn app.main:app --reload

Then, from the project root:

    python -m scripts.scan path/to/photo.jpg
    python -m scripts.scan path/to/photo.jpg --open      # open the top match in a browser
    python -m scripts.scan path/to/photo.jpg --add       # add an exact match to the collection
    python -m scripts.scan path/to/photo.jpg --api-base http://raspberrypi.local:8000

Order of operations: the footer ("SET · 123/280") is read first and looked up
by set code + collector number, which identifies exactly one printing. Only if
that read is missing, low-confidence, or matches nothing is the title read and
used for a name search that the user picks from.

Name-extraction (the real accuracy lever, per ROADMAP.md Phase 6):
the original test_API.py spike just took `results[0]` - EasyOCR's first
detected box, in whatever order it happened to find text - and stripped
trailing digits/braces with a regex. Two things replace that:

- **Box selection**: a Magic card's title is the largest text near the top of
  the card, not necessarily the first box EasyOCR returns. This picks the
  highest-confidence box among those in the top ~20% of the image, breaking
  ties by bounding-box area (title text is set larger than everything else
  up there).
- **Cleanup**: strip a leading mana-cost cluster (e.g. "{2}{U}") and a
  trailing mana-cost cluster some OCR misreads glue onto the name, not just
  trailing digits.

Set code / collector number: the tiny footer line is a hard OCR target, so
boxes under a confidence threshold are ignored and a miss simply falls
through to the title path - a wrong guess costs one extra lookup, never a
wrong card, because set code + number must resolve to exactly one printing.
"""
from __future__ import annotations

import argparse
import os
import re
import webbrowser

import httpx

# Parsing + decision logic is shared with the Pi receiver (app/scans.py).
# clean_name / find_set_and_number / pick_name_box are re-exported for tests.
from app.scans import clean_name, find_set_and_number, identify, pick_name_box  # noqa: F401

# One OCR pass over the whole photo, at this width. Measured on real photos:
# ~5-6s on CPU, and still large enough to read the tiny footer line. Going
# lower (800px) or cropping to bands loses the footer; the card rarely fills
# the frame (toploaders, hands, a table), so image-relative bands miss it.
_TARGET_WIDTH = 1200

_reader = None


# EasyOCR's model download + init is slow (several seconds) - only pay that
# cost if this script is actually run, not on every `import scripts.scan`,
# and only once per process.
def _get_reader():
    global _reader
    if _reader is None:
        import easyocr

        _reader = easyocr.Reader(["en"], gpu=False)
    return _reader


def _load_image(image_path: str):
    """Read a card photo and resize it to _TARGET_WIDTH wide. Small photos are
    upscaled, not left alone: the footer text is only legible to EasyOCR at
    roughly this scale (a 768px-wide photo read nothing until upscaled)."""
    import cv2

    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"no such file: {image_path}")
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError(f"could not read image: {image_path}")
    h, w = img.shape[:2]
    if w != _TARGET_WIDTH:
        interp = cv2.INTER_AREA if w > _TARGET_WIDTH else cv2.INTER_CUBIC
        img = cv2.resize(img, (_TARGET_WIDTH, round(h * _TARGET_WIDTH / w)), interpolation=interp)
    return img


def _read_all(img) -> list[tuple[list, str, float]]:
    return [(bbox, text, float(conf)) for bbox, text, conf in _get_reader().readtext(img)]


def _api_search(api_base: str, params: dict) -> list[dict]:
    resp = httpx.get(f"{api_base}/api/search", params=params, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("error") == "offline":
        raise RuntimeError(
            "the app is offline and this card isn't cached yet - connect to wifi and retry"
        )
    return data


def add_to_collection(api_base: str, card_id: str, quantity: int = 1) -> None:
    """POST to the same form route the web UI uses; the app redirects (303) to /collection."""
    resp = httpx.post(
        f"{api_base}/collection",
        data={"card_id": card_id, "quantity": quantity, "finish": "nonfoil"},
        timeout=30.0,
    )
    # httpx doesn't follow redirects by default; 303 is the app's success response.
    if resp.status_code != 303:
        resp.raise_for_status()
        raise RuntimeError(f"unexpected response adding card: {resp.status_code}")


def scan(image_path: str, api_base: str) -> tuple[list[dict], bool]:
    """Identify a card photo. Returns (candidates, exact).

    exact=True means the footer's set code + collector number resolved to
    exactly one printing - no need to ask the user. Otherwise (footer missing,
    fuzzy, or no such printing) candidates come from a name search on the
    title, for the user to pick from. Both come from one OCR pass over the
    photo; the footer lookup just takes priority, and the title search only
    runs if it doesn't settle the card.
    """
    boxes = _read_all(_load_image(image_path))

    def search(text: str | None = None, **params) -> list[dict]:
        if text:
            params["q"] = text
        return _api_search(api_base, params)

    result = identify(boxes, search)
    return result["candidates"], result["exact"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", help="path to a photo of the card")
    parser.add_argument(
        "--api-base", default="http://localhost:8000", help="base URL of the running web app"
    )
    parser.add_argument(
        "--open", action="store_true", help="open the top match's card page in a browser"
    )
    parser.add_argument(
        "--add",
        action="store_true",
        help="add the match to the collection (only for an exact set+number match) and open it",
    )
    parser.add_argument("--quantity", type=int, default=1, help="copies to add with --add")
    args = parser.parse_args()

    candidates, exact = scan(args.image, args.api_base)
    if not candidates:
        print("No candidates found - OCR couldn't read a usable set/number or name.")
        return

    if exact:
        print("Exact match (set code + collector number):\n")
    else:
        print(f"{len(candidates)} candidate(s) from the title - pick one:\n")
    for c in candidates:
        print(
            f"  {c['name']:<30} {c['set_code'] or '?':<5} "
            f"#{c['collector_number'] or '?':<6} ${c['price_usd'] or 'n/a'}"
        )

    if args.add:
        if not exact:
            print(
                "\nNot added: only an exact set+number match is added automatically. "
                "Pick a card from the list in the web UI."
            )
        else:
            add_to_collection(args.api_base, candidates[0]["id"], args.quantity)
            print(f"\nAdded {args.quantity}x {candidates[0]['name']} to your collection.")
            webbrowser.open(f"{args.api_base}/collection")
            return

    if args.open:
        webbrowser.open(f"{args.api_base}/cards/{candidates[0]['id']}")


if __name__ == "__main__":
    main()
