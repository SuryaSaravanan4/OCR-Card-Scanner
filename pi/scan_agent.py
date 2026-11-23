"""The Pi side of the browser's "Scan stack" button.

Runs on the Pi and waits. About once a second it asks the PC
(`GET /api/pi/command`) whether the browser clicked "Scan stack". When it did, the
Pi scans the whole stack by itself:

    for each card:  feed (A + B) -> photo -> OCR -> POST to the PC -> eject (B, reversed)

Retries (the card is NOT moved between tries; a new photo is taken each time):
  * no readable text   -> retry; still nothing after --attempts tries = no card in
                          front of the camera (stack empty, or a jam): the run ends,
                          nothing is ejected.
  * PC found no card   -> retry; still nothing = the card is reported as failed (the
                          browser shows it), ejected, and the next card is fed.
  * PC unreachable     -> retry the POST a few times, then the run ends.
  * camera error       -> retry, then the run ends and the error is reported.
The browser's "Stop" is checked before each new card. The servos are stopped at the
end, on any error, on Ctrl+C, and if the SSH session to the Pi drops.

Normally started from the PC with scripts/pi_link.ps1, which also opens the SSH
tunnel that lets the Pi reach the PC at http://127.0.0.1:8000. By hand, on the Pi:

    python -m pi.scan_agent                     # mock servos (nothing moves)
    python -m pi.scan_agent --real-motor        # real servos
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path
from typing import Callable

from pi.autoscan import CaptureError, capture_photo, has_text, make_scan_fn, make_send_fn
from pi.feeder import FeederError, build_feeder, describe_motion, load_settings
from pi.one_card import DEFAULT_CONFIG

DEFAULT_PC_URL = "http://127.0.0.1:8000"  # through the SSH tunnel from scripts/pi_link.ps1
PHOTO_PATH = Path.home() / "stack_card.jpg"


def run_stack(
    feeder,
    capture_fn: Callable[[], None],
    scan_fn: Callable[[], dict],
    send_fn: Callable[[dict, dict], dict | None],
    event_fn: Callable[[dict], None],
    should_stop: Callable[[], bool],
    run_id: int,
    max_cards: int = 60,
    attempts: int = 3,
    send_tries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = print,
) -> dict:
    """Scan cards until the stack is empty, Stop is pressed, or something fails.

    Returns {"reason": why it ended, "cards": cards fed, "identified", "failed", "message"}.
    `reason` is one of: no_card, stopped, max_cards, pc_unreachable, camera_error,
    interrupted. The feeder is always stopped before this returns or raises.
    """
    result = {"reason": "max_cards", "cards": 0, "identified": 0, "failed": 0, "message": None}

    def send(scan: dict, card_no: int, attempt: int) -> dict | None:
        extra = {"run_id": run_id, "card_no": card_no, "attempt": attempt}
        for i in range(send_tries):
            reply = send_fn(scan, extra)
            if reply is not None:
                return reply
            if i + 1 < send_tries:
                sleep(2.0)
        return None

    try:
        for card_no in range(1, max_cards + 1):
            if should_stop():
                result["reason"] = "stopped"
                break
            say(f"[card {card_no}] feeding")
            feeder.feed_cycle()
            result["cards"] = card_no

            saw_text = identified = False
            failure = "could not identify the card"
            end_run = None
            for attempt in range(1, attempts + 1):
                try:
                    capture_fn()
                except (CaptureError, OSError) as exc:
                    say(f"[card {card_no}] camera failed (try {attempt}/{attempts}): {exc}")
                    if attempt == attempts:
                        end_run = ("camera_error", str(exc))
                    continue
                scan = scan_fn()
                if not has_text(scan["detections"]):
                    say(f"[card {card_no}] no readable text (try {attempt}/{attempts})")
                    continue
                saw_text = True
                reply = send(scan, card_no, attempt)
                if reply is None:
                    end_run = ("pc_unreachable", "could not reach the PC")
                    break
                if reply.get("exact") or reply.get("candidates", 0) > 0:
                    identified = True
                    say(f"[card {card_no}] identified (try {attempt})")
                    break
                if reply.get("offline"):
                    failure = "the PC is offline (no internet for Scryfall)"
                    break  # retrying won't help
                say(f"[card {card_no}] PC found no card (try {attempt}/{attempts})")

            if end_run:
                result["reason"], result["message"] = end_run
                if end_run[0] == "camera_error":
                    event_fn({"event": "error", "card_no": card_no, "message": f"camera failed: {end_run[1]}"})
                break
            if not saw_text:
                result["reason"] = "no_card"
                result["cards"] = card_no - 1  # nothing was actually there
                say(f"[card {card_no}] no card in front of the camera: stack empty or jammed")
                break
            if identified:
                result["identified"] += 1
            else:
                result["failed"] += 1
                event_fn({"event": "card_failed", "card_no": card_no,
                          "message": f"{failure} after {attempts} tries"})
            say(f"[card {card_no}] ejecting")
            feeder.clear()
    except KeyboardInterrupt:
        result["reason"] = "interrupted"
        say("Interrupted: stopping the servos.")
    finally:
        feeder.stop()
    return result


# ------------------------------------------------------------------ PC link


def _get_json(url: str, timeout: float = 5.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (OSError, ValueError):
        return None


def _post_json(url: str, body: dict, timeout: float = 10.0) -> bool:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except OSError:
        return False


def serve(
    get_command: Callable[[bool], dict | None],
    start_run: Callable[[int], dict],
    post_event: Callable[[dict], bool],
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = print,
    max_polls: int | None = None,
) -> None:
    """Poll the PC forever; run a stack whenever the browser asks. `max_polls` is for tests."""
    reachable = None
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        command = get_command(False)
        if command is None:
            if reachable is not False:
                say("Cannot reach the PC app. Is it running, and is the SSH tunnel open? Retrying...")
            reachable = False
            sleep(3.0)
            continue
        if reachable is not True:
            say("Connected to the PC. Waiting for 'Scan stack' in the browser...")
        reachable = True
        if command.get("command") == "start":
            run_id = command["run_id"]
            say(f"=== run {run_id}: scanning the stack ===")
            result = start_run(run_id)
            say(f"=== run {run_id} ended: {result['reason']} ({result['cards']} cards, "
                f"{result['identified']} identified, {result['failed']} failed) ===")
            post_event({"run_id": run_id, "event": "finished",
                        "reason": result["reason"], "message": result["message"]})
            if result["reason"] == "interrupted":
                return
        sleep(1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait for 'Scan stack' in the browser and scan the stack.")
    parser.add_argument("--real-motor", action="store_true", help="drive the real servos (default: mock)")
    parser.add_argument("--pc-url", default=DEFAULT_PC_URL, help="the PC app's address (default: through the SSH tunnel)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="feeder settings JSON")
    parser.add_argument("--attempts", type=int, default=3, help="tries per card before giving up")
    parser.add_argument("--max-cards", type=int, default=60, help="safety cap on cards per run")
    parser.add_argument("--speed", type=float, help="servo speed 0..1 (capped by max_speed)")
    parser.add_argument("--feed-s", type=float, help="seconds the servos push the card in")
    parser.add_argument("--settle-s", type=float, help="seconds to wait before the photo")
    parser.add_argument("--clear-s", type=float, help="seconds the servos push the card out")
    parser.add_argument("--image", type=Path, help="OCR this saved photo instead of using the camera (testing)")
    parser.add_argument("--no-footer", action="store_true", help="OCR the title band only")
    args = parser.parse_args()

    if args.attempts < 1 or args.max_cards < 1:
        print("--attempts and --max-cards must be at least 1")
        return 2
    if not args.config.exists():
        print(f"Config not found: {args.config}")
        return 2
    settings = load_settings(args.config)
    for name in ("speed", "feed_s", "settle_s", "clear_s"):
        value = getattr(args, name)
        if value is not None:
            setattr(settings.feed, name, value)
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
    print(f"speed {f.speed} | feed {f.feed_s} s ({describe_motion(f.feed_motors)}) | settle {f.settle_s} s | "
          f"eject {f.clear_s} s ({describe_motion(f.clear_motors)}) | "
          f"{args.attempts} tries per card | max {args.max_cards} cards")

    base = args.pc_url.rstrip("/")
    photo = args.image or PHOTO_PATH
    capture_fn = (lambda: None) if args.image else (lambda: capture_photo(photo))

    try:
        import easyocr

        t = time.perf_counter()
        print("Loading OCR model (once)...")
        reader = easyocr.Reader(["en"], gpu=False)
        print(f"OCR ready ({time.perf_counter() - t:.1f} s).")
        scan_fn = make_scan_fn(reader, photo, read_footer=not args.no_footer)
        send_fn = make_send_fn(f"{base}/api/scan")

        def get_command(busy: bool) -> dict | None:
            return _get_json(f"{base}/api/pi/command?busy={int(busy)}")

        def post_event(event: dict) -> bool:
            return _post_json(f"{base}/api/pi/event", event)

        def start_run(run_id: int) -> dict:
            def should_stop() -> bool:
                command = get_command(True)
                return bool(command and command.get("command") == "stop")

            return run_stack(
                feeder, capture_fn, scan_fn, send_fn,
                event_fn=lambda e: post_event({"run_id": run_id, **e}),
                should_stop=should_stop, run_id=run_id,
                max_cards=args.max_cards, attempts=args.attempts,
            )

        serve(get_command, start_run, post_event)
    except KeyboardInterrupt:
        print("\nScanner stopped.")
    finally:
        feeder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
