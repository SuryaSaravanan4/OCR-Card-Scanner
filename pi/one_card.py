"""One-card test: both servos feed a card to the camera, the Pi OCRs it and sends it to
the PC (the browser's /scan page fills in by itself), then on Enter both servos push the
card out the far side.

    python -m pi.one_card                    # mock servos (nothing moves), real camera
    python -m pi.one_card --real-motor       # real servos A (GPIO18) and B (GPIO19)
    python -m pi.one_card --real-motor --feed-s 1.2 --clear-s 2.5   # tune without editing files

Run from the folder that holds `pi/`. Steps:
  1. load the OCR model (slow, done first so the card does not wait in front of the camera)
  2. Enter -> servos A and B spin together for feed_s, then settle_s pause
  3. photo -> OCR (title + footer, about a minute on the Pi 4) -> POST to the PC
  4. Enter -> servos A and B spin together, same direction, for clear_s: the card leaves
The servos are stopped at the end, on any error, and on Ctrl+C.

The photo is saved to ~/one_card.jpg so it does not overwrite ~/magic_small.jpg.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Callable

from pi.autoscan import (
    DEFAULT_PC_URL,
    CaptureError,
    capture_photo,
    has_text,
    make_scan_fn,
    make_send_fn,
)
from pi.feeder import FeederError, build_feeder, load_settings

DEFAULT_CONFIG = Path(__file__).resolve().parent / "feeder_config.two_servo.json"
PHOTO_PATH = Path.home() / "one_card.jpg"


def describe_reply(reply: dict | None) -> str:
    """One line about what the PC made of the scan."""
    if reply is None:
        return "PC: NOT reached (check the PC app is running and --pc-url)"
    if reply.get("offline"):
        return f"PC: scan #{reply.get('id')} saved, but the PC is offline from Scryfall"
    if reply.get("exact"):
        return f"PC: scan #{reply.get('id')} -> exact match (set code + collector number)"
    n = reply.get("candidates", 0)
    if n:
        return f"PC: scan #{reply.get('id')} -> {n} candidate(s) by title"
    return f"PC: scan #{reply.get('id')} -> no match"


def run_one_card(
    feeder,
    capture_fn: Callable[[], None],
    scan_fn: Callable[[], dict],
    send_fn: Callable[[dict], dict | None],
    wait_fn: Callable[[str], None],
    say: Callable[[str], None] = print,
) -> dict:
    """feed -> capture -> OCR -> POST -> wait for the operator -> eject.

    `wait_fn(prompt)` blocks until the operator says go (Enter on the Pi).
    Returns {"stage": how far it got, "feed_ok", "reply", "feed_s", "clear_s"}.
    The servos are always stopped before this returns or raises.
    """
    result: dict = {"stage": "start", "feed_ok": False, "reply": None, "feed_s": 0.0, "clear_s": 0.0}
    try:
        wait_fn("Card loaded? Press Enter to FEED (servos A + B together)... ")
        feed = feeder.feed_cycle()
        result["feed_s"] = feed["feed_s"]
        result["stage"] = "fed"
        say(f"Fed for {feed['feed_s']:.2f} s, settled {feed['settle_s']:.2f} s. Taking the photo...")

        capture_fn()
        say("Reading the card (OCR)...")
        t = time.perf_counter()
        scan = scan_fn()
        result["feed_ok"] = has_text(scan["detections"])
        result["stage"] = "scanned"
        texts = [d["text"] for d in scan["detections"]]
        say(f"OCR took {time.perf_counter() - t:.1f} s, read {len(texts)} piece(s) of text: {texts}")
        if not result["feed_ok"]:
            say("WARNING: no readable text. The card may not be in front of the camera.")

        reply = send_fn(scan)
        result["reply"] = reply
        result["stage"] = "sent"
        say(describe_reply(reply))

        wait_fn("Check the browser (/scan). Press Enter to EJECT the card... ")
        result["clear_s"] = feeder.clear()
        result["stage"] = "ejected"
        say(f"Ejected for {result['clear_s']:.2f} s.")
    except KeyboardInterrupt:
        say(f"Interrupted after stage '{result['stage']}': stopping the servos.")
        result["interrupted"] = True
    finally:
        feeder.stop()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="One-card feed, scan, eject test for the Pi.")
    parser.add_argument("--real-motor", action="store_true", help="drive the real servos (default: mock)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="feeder settings JSON")
    parser.add_argument("--speed", type=float, help="servo speed 0..1 (capped by max_speed in the config)")
    parser.add_argument("--feed-s", type=float, help="seconds the servos push the card in")
    parser.add_argument("--settle-s", type=float, help="seconds to wait before the photo")
    parser.add_argument("--clear-s", type=float, help="seconds the servos push the card out")
    parser.add_argument("--image", type=Path, help="OCR this saved photo instead of using the camera")
    parser.add_argument("--pc-url", default=DEFAULT_PC_URL, help="where to POST the scan")
    parser.add_argument("--no-footer", action="store_true", help="OCR the title band only")
    args = parser.parse_args()

    if not args.config.exists():
        print(f"Config not found: {args.config}")
        return 2
    settings = load_settings(args.config)
    for flag, attr in (("speed", "speed"), ("feed_s", "feed_s"), ("settle_s", "settle_s"), ("clear_s", "clear_s")):
        value = getattr(args, flag)
        if value is not None:
            setattr(settings.feed, attr, value)
    try:
        settings.feed.validate()
        for cal in settings.calibrations:
            cal.validate()
        feeder = build_feeder(settings, real_motor=args.real_motor)
    except (ValueError, FeederError, RuntimeError, OSError) as error:
        print(f"Cannot set up the servos: {error}")
        return 2
    feeder.install_safety_handlers()

    f = settings.feed
    print(f"Servos: {'REAL' if args.real_motor else 'MOCK (nothing will move)'} | "
          + ", ".join(f"{n} on GPIO{p}" for n, p in zip(settings.names, settings.pins)))
    print(f"speed {f.speed} | feed {f.feed_s} s | settle {f.settle_s} s | eject {f.clear_s} s (same direction)")

    photo = args.image or PHOTO_PATH
    capture_fn = (lambda: None) if args.image else (lambda: capture_photo(photo))

    try:
        import easyocr

        t = time.perf_counter()
        print("Loading OCR model (once)...")
        reader = easyocr.Reader(["en"], gpu=False)
        print(f"OCR ready ({time.perf_counter() - t:.1f} s).")

        result = run_one_card(
            feeder,
            capture_fn,
            make_scan_fn(reader, photo, read_footer=not args.no_footer),
            make_send_fn(args.pc_url),
            wait_fn=input,
        )
    except CaptureError as error:
        print(f"Camera capture failed: {error}")
        print("The card may still be in front of the camera. Remove it by hand.")
        return 1
    finally:
        feeder.close()

    print(f"Done. Reached stage: {result['stage']}. Photo: {photo}")
    return 0 if result["stage"] == "ejected" else 1


if __name__ == "__main__":
    raise SystemExit(main())
