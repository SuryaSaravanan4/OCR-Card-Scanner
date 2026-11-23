"""Automatic scan loop for the Raspberry Pi: servos feed a card, the camera photographs
it, EasyOCR reads the title and footer, the PC identifies it, and the servos clear it.

    python -m pi.autoscan --cards 3                        # mock servos, real camera
    python -m pi.autoscan --cards 3 --image ~/magic_small.jpg   # mock servos, saved photo
    python -m pi.autoscan --cards 10 --real-motor          # real servos (after bench setup)

Run from the repository root. By default the servos are MOCKS that move nothing; real
servos need --real-motor and the wiring/calibration steps in pi/README.md.

Per cycle:  feed -> settle -> capture -> OCR -> POST to the PC -> clear -> log.
A cycle's feed counts as OK when OCR read some text (the camera is the only check;
it cannot tell one card from two stuck together). The loop stops after --cards cards,
after --max-empty empty scans in a row (no cards left, or a jam), or on Ctrl+C. The
servos are always stopped at the end.

The OCR and photo code is adapted from evidence/pi/pi_scan_footer.py, which stays
untouched as the record of what ran on the Pi.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

from pi.feeder import FeederError, build_feeder, load_settings

HOME = Path.home()
IMAGE_PATH = HOME / "magic_small.jpg"
TITLE_CROP_PATH = HOME / "name_crop.jpg"
FOOTER_CROP_PATH = HOME / "footer_crop.jpg"
CYCLE_LOG = HOME / "autoscan_cycles.csv"
DEFAULT_PC_URL = "http://192.168.137.1:8000/api/scan"

CAPTURE_WIDTH = 1920
CAPTURE_HEIGHT = 1080
# Bands are fractions of the photo height (0 = top, 1 = bottom), full width. The footer
# band starts at 0.65 because the footer of a card photographed on the Pi sat ~77-81%
# of the way down; check footer_crop.jpg if reads are poor.
TITLE_BAND = (0.0, 0.5)
FOOTER_BAND = (0.65, 1.0)

# A detection counts as "read some text" only above this OCR confidence, so a blank
# tray (which can produce junk at ~0) is not mistaken for a fed card.
MIN_TEXT_CONF = 0.3

CYCLE_FIELDS = [
    "cycle_no", "time", "feed_s", "settle_s", "capture_s", "load_crop_s",
    "ocr_title_s", "ocr_footer_s", "ocr_s", "post_s", "clear_s", "total_s",
    "feed_ok", "detections", "identified", "exact", "tally",
    "mem_avail_mb", "swap_used_mb",
]


# ------------------------------------------------------------------ pure logic


def has_text(detections: list[dict], min_conf: float = MIN_TEXT_CONF) -> bool:
    """True if OCR read at least one real piece of text (not empty, not near-zero confidence)."""
    return any(d.get("text", "").strip() and d.get("confidence", 0.0) >= min_conf for d in detections)


def run_cycles(
    feeder,
    capture_fn: Callable[[], None],
    scan_fn: Callable[[], dict],
    send_fn: Callable[[dict], dict | None],
    log_fn: Callable[[dict], None],
    cards: int,
    max_empty: int = 3,
    tally_fn: Callable[[int], str | None] | None = None,
    clock: Callable[[], float] = time.perf_counter,
    say: Callable[[str], None] = print,
) -> dict:
    """The feed -> capture -> OCR -> POST -> clear loop. Everything it needs is passed in.

    `scan_fn()` returns {"detections": [...], "ocr_s": ..., plus optional timing fields}.
    `send_fn(scan)` returns the PC's JSON reply (or None if the POST failed).
    Returns {"rows": [...], "stopped": "done" | "no_cards" | "interrupted"}.
    The feeder is always stopped before this returns.
    """
    rows: list[dict] = []
    empty_in_a_row = 0
    stopped = "done"
    try:
        for n in range(1, cards + 1):
            t_cycle = clock()
            feed = feeder.feed_cycle()

            t = clock()
            capture_fn()
            capture_s = clock() - t

            scan = scan_fn()
            feed_ok = has_text(scan["detections"])

            t = clock()
            reply = send_fn(scan)
            post_s = clock() - t
            identified = bool(reply and (reply.get("exact") or reply.get("candidates", 0) > 0))
            exact = bool(reply and reply.get("exact"))

            tally = tally_fn(n) if tally_fn else None

            clear_s = feeder.clear()

            row = {
                "cycle_no": n,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "feed_s": round(feed["feed_s"], 2),
                "settle_s": round(feed["settle_s"], 2),
                "capture_s": round(capture_s, 2),
                "load_crop_s": round(scan.get("load_crop_s", 0.0), 2),
                "ocr_title_s": round(scan.get("ocr_title_s", 0.0), 2),
                "ocr_footer_s": round(scan.get("ocr_footer_s", 0.0), 2),
                "ocr_s": round(scan.get("ocr_s", 0.0), 2),
                "post_s": round(post_s, 2),
                "clear_s": round(clear_s, 2),
                "total_s": round(clock() - t_cycle, 2),
                "feed_ok": feed_ok,
                "detections": len(scan["detections"]),
                "identified": identified,
                "exact": exact,
                "tally": tally,
                "mem_avail_mb": scan.get("mem_avail_mb"),
                "swap_used_mb": scan.get("swap_used_mb"),
            }
            rows.append(row)
            log_fn(row)
            say(
                f"[cycle {n}/{cards}] feed {row['feed_s']}s | capture {row['capture_s']}s | "
                f"OCR {row['ocr_s']}s | POST {row['post_s']}s | clear {row['clear_s']}s | "
                f"TOTAL {row['total_s']}s | text read: {'yes' if feed_ok else 'NO'} | "
                f"identified: {'exact' if exact else ('candidates' if identified else 'no')}"
            )

            if feed_ok:
                empty_in_a_row = 0
            else:
                empty_in_a_row += 1
                if empty_in_a_row >= max_empty:
                    stopped = "no_cards"
                    say(f"Stopping: {max_empty} empty scans in a row (no cards left, or a jam).")
                    break
    except KeyboardInterrupt:
        stopped = "interrupted"
        say("Interrupted: stopping the servos.")
    finally:
        feeder.stop()
    return {"rows": rows, "stopped": stopped}


def summarize(rows: list[dict]) -> dict:
    """Averages and rates over the logged cycles."""
    n = len(rows)
    if n == 0:
        return {"cycles": 0}

    def avg(key: str) -> float:
        return sum(r[key] for r in rows) / n

    total_time = sum(r["total_s"] for r in rows)
    tallied = [r for r in rows if r["tally"] in ("y", "n")]
    return {
        "cycles": n,
        "feed_ok": sum(1 for r in rows if r["feed_ok"]),
        "identified": sum(1 for r in rows if r["identified"]),
        "exact": sum(1 for r in rows if r["exact"]),
        "avg_feed_s": avg("feed_s"),
        "avg_capture_s": avg("capture_s"),
        "avg_ocr_s": avg("ocr_s"),
        "avg_post_s": avg("post_s"),
        "avg_clear_s": avg("clear_s"),
        "avg_total_s": avg("total_s"),
        "cards_per_min": n / (total_time / 60) if total_time > 0 else 0.0,
        "tally_yes": sum(1 for r in tallied if r["tally"] == "y"),
        "tally_total": len(tallied),
    }


def print_summary(rows: list[dict], stopped: str, say: Callable[[str], None] = print) -> None:
    s = summarize(rows)
    say("\n===== AUTOSCAN SUMMARY =====")
    if s["cycles"] == 0:
        say("no cycles ran")
        return
    say(f"stopped because         : {stopped}")
    say(f"cycles                  : {s['cycles']}")
    say(f"text read (camera check): {s['feed_ok']} of {s['cycles']}")
    say(f"identified by the PC    : {s['identified']} of {s['cycles']} ({s['exact']} exact footer matches)")
    if s["tally_total"]:
        say(f"operator tally (y = one card fed correctly): {s['tally_yes']} of {s['tally_total']}")
    say(f"avg feed / capture / OCR / POST / clear : {s['avg_feed_s']:.2f} / {s['avg_capture_s']:.2f} / "
        f"{s['avg_ocr_s']:.2f} / {s['avg_post_s']:.2f} / {s['avg_clear_s']:.2f} s")
    say(f"avg total per card      : {s['avg_total_s']:.2f} s (hands-free, feed to clear)")
    say(f"cards per minute        : {s['cards_per_min']:.2f}")


# ------------------------------------------------------- hardware / OCR helpers
# cv2 and easyocr are imported inside these functions so the pure logic above (and the
# tests) load without them.


def mem_status() -> tuple[float | None, float | None]:
    """(MemAvailable MB, swap used MB) from /proc/meminfo; (None, None) if unreadable."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, value = line.split(":", 1)
                info[key] = int(value.split()[0])
        return info["MemAvailable"] / 1024, (info["SwapTotal"] - info["SwapFree"]) / 1024
    except (OSError, KeyError, ValueError):
        return None, None


class CaptureError(RuntimeError):
    """The photo could not be taken (no camera, or not running on a Pi)."""


def capture_photo(path: Path = IMAGE_PATH) -> None:
    """Take a photo with the Pi camera. Raises CaptureError with a plain explanation."""
    try:
        subprocess.run(
            [
                "rpicam-still", "--timeout", "1000",
                "--width", str(CAPTURE_WIDTH), "--height", str(CAPTURE_HEIGHT),
                "--quality", "85", "--output", str(path), "--nopreview",
            ],
            check=True,
        )
    except FileNotFoundError as exc:
        raise CaptureError(
            "rpicam-still was not found. The camera only works on the Raspberry Pi; "
            "on a PC add --image <photo.jpg> to reuse a saved photo."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise CaptureError(
            f"the camera command failed ({exc}). Check it with: rpicam-hello --list-cameras"
        ) from exc


def read_band(reader, image, band: tuple[float, float], crop_path: Path):
    """OCR one horizontal band of the photo. Returns (detections, ocr_s, crop_s).

    Box y-coordinates are shifted to full-photo coordinates so the PC can tell the
    footer (low in the photo) from the title (high in the photo).
    """
    import cv2

    t_crop = time.perf_counter()
    height = image.shape[0]
    y1 = max(0, min(height, int(round(height * band[0]))))
    y2 = max(0, min(height, int(round(height * band[1]))))
    crop = image[y1:y2, :]
    if crop.size == 0:
        raise ValueError(f"Crop is empty. Band {band} on a photo {height} px high")
    cv2.imwrite(str(crop_path), crop)
    crop_s = time.perf_counter() - t_crop

    t_ocr = time.perf_counter()
    result = reader.readtext(crop)
    ocr_s = time.perf_counter() - t_ocr

    detections = [
        {
            "text": text,
            "confidence": float(confidence),
            "box": [[float(x), float(y) + y1] for x, y in box],
        }
        for box, text, confidence in result
    ]
    return detections, ocr_s, crop_s


def make_scan_fn(reader, image_path: Path, read_footer: bool) -> Callable[[], dict]:
    """A function that OCRs the current photo (title band, plus footer band if asked)."""
    import cv2

    def scan() -> dict:
        t = time.perf_counter()
        image = cv2.imread(str(image_path))
        if image is None:
            raise FileNotFoundError(f"Could not load image: {image_path}")
        load_s = time.perf_counter() - t

        bands = [(TITLE_BAND, TITLE_CROP_PATH)]
        if read_footer:
            bands.append((FOOTER_BAND, FOOTER_CROP_PATH))

        mem_avail, swap_used = mem_status()  # just before OCR: shows whether the Pi is swapping
        detections: list[dict] = []
        ocr_times: list[float] = []
        crop_total = 0.0
        for band, crop_path in bands:
            found, ocr_s, crop_s = read_band(reader, image, band, crop_path)
            detections.extend(found)
            ocr_times.append(ocr_s)
            crop_total += crop_s

        return {
            "detections": detections,
            "ocr_s": sum(ocr_times),
            "ocr_title_s": ocr_times[0],
            "ocr_footer_s": ocr_times[1] if len(ocr_times) > 1 else 0.0,
            "load_crop_s": load_s + crop_total,
            "mem_avail_mb": None if mem_avail is None else round(mem_avail),
            "swap_used_mb": None if swap_used is None else round(swap_used),
        }

    return scan


def make_send_fn(pc_url: str) -> Callable[[dict], dict | None]:
    """A function that POSTs a scan to the PC and returns the PC's JSON reply (or None).

    `extra` (optional) is merged into the payload, e.g. run_id / card_no / attempt.
    """

    def send(scan: dict, extra: dict | None = None) -> dict | None:
        payload = {
            "type": "card_scan",
            "detections": [
                {"text": d["text"], "confidence": d["confidence"], "box": d["box"]}
                for d in scan["detections"]
            ],
            "elapsed_seconds": round(scan.get("ocr_s", 0.0), 2),
            **(extra or {}),
        }
        request = urllib.request.Request(
            pc_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode()
            print(f"  PC replied: {body}")
            return json.loads(body)
        except (OSError, ValueError) as error:
            print(f"  Could not send results to PC: {error}")
            return None

    return send


def make_csv_logger(path: Path) -> Callable[[dict], None]:
    def log(row: dict) -> None:
        new_file = not Path(path).exists()
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CYCLE_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)

    return log


def ask_tally(n: int) -> str | None:
    answer = input(f"  Cycle {n}: was exactly ONE card fed correctly? [y/n, Enter = skip]: ").strip().lower()
    return answer if answer in ("y", "n") else None


# ------------------------------------------------------------------------ main


def main() -> int:
    parser = argparse.ArgumentParser(description="Automatic card feed + scan loop for the Pi.")
    parser.add_argument("--cards", type=int, default=10, help="how many cards to scan (default 10)")
    parser.add_argument("--max-empty", type=int, default=3, help="stop after this many empty scans in a row")
    parser.add_argument("--real-motor", action="store_true", help="drive the real servos (default: mock)")
    parser.add_argument("--no-footer", action="store_true", help="OCR the title band only")
    parser.add_argument("--image", type=Path, help="reuse this saved photo instead of the camera")
    parser.add_argument("--pc-url", default=DEFAULT_PC_URL, help="where to POST scans")
    parser.add_argument("--config", type=Path, help="feeder settings JSON (see pi/feeder_config.example.json)")
    parser.add_argument("--speed", type=float, help="override the configured servo speed (still capped by max_speed)")
    parser.add_argument("--tally", action="store_true", help="ask y/n after each cycle for a hand-checked count")
    parser.add_argument("--log", type=Path, default=CYCLE_LOG, help="CSV file to append cycle rows to")
    args = parser.parse_args()

    settings = load_settings(args.config)
    if args.speed is not None:
        settings.feed.speed = args.speed
    try:
        settings.feed.validate()
        for cal in settings.calibrations:
            cal.validate()
        feeder = build_feeder(settings, real_motor=args.real_motor)
    except ImportError:
        print("gpiozero is not installed. On the Pi: pip install -r pi/requirements-pi.txt")
        return 2
    except (ValueError, FeederError) as error:
        print(f"Bad feeder settings: {error}")
        return 2
    feeder.install_safety_handlers()
    print(f"Servos: {'REAL' if args.real_motor else 'MOCK (nothing will move)'}")

    image_path = args.image or IMAGE_PATH
    capture_fn = (lambda: None) if args.image else (lambda: capture_photo(image_path))

    import easyocr

    t = time.perf_counter()
    print("Loading OCR model once...")
    reader = easyocr.Reader(["en"], gpu=False)
    print(f"OCR ready. (model load took {time.perf_counter() - t:.1f} s, paid once)")
    scan_fn = make_scan_fn(reader, image_path, read_footer=not args.no_footer)

    try:
        result = run_cycles(
            feeder,
            capture_fn,
            scan_fn,
            make_send_fn(args.pc_url),
            make_csv_logger(args.log),
            cards=args.cards,
            max_empty=args.max_empty,
            tally_fn=ask_tally if args.tally else None,
        )
    except CaptureError as error:
        print(f"Camera capture failed: {error}")
        return 1
    finally:
        feeder.close()

    print_summary(result["rows"], result["stopped"])
    print(f"log file: {args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
