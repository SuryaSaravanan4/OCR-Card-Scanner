import cv2
import easyocr
import time
import subprocess
import json
import urllib.request
import csv
import os
from datetime import datetime


# ---------------------------------------------------------------------------
# READ_FOOTER = True : OCR the title band AND the footer band (set code +
#                      collector number), so the PC can identify the exact
#                      printing ("exact match") instead of only guessing by name.
# READ_FOOTER = False: OCR the title band only (the original behaviour).
# Run the same 10 cards in both modes to see what footer reading costs.
# ---------------------------------------------------------------------------
READ_FOOTER = True

IMAGE_PATH = "/home/pi-model-b/magic_small.jpg"
CROP_PATH = "/home/pi-model-b/name_crop.jpg"
FOOTER_CROP_PATH = "/home/pi-model-b/footer_crop.jpg"  # open this to see what the camera saw

CAPTURE_WIDTH = 1920
CAPTURE_HEIGHT = 1080

# Bands are FRACTIONS of the photo height (0.0 = top, 1.0 = bottom), full width.
# TITLE_BAND (0.0, 0.5) is rows 0-540 of a 1080-high photo: the same as the old crop.
# FOOTER_BAND is the bottom of the card. In the Pi photo of 2026-11-10 (Demonic Tutor,
# CMR #656) the footer text sat at about 77-81% of the way down, so the band starts at
# 0.65 (row 702) to leave margin for a card that sits a little higher or lower.
# If the footer is cut off, adjust these two numbers and look at footer_crop.jpg.
TITLE_BAND = (0.0, 0.5)
FOOTER_BAND = (0.65, 1.0)

# PC receiver settings (home Ethernet connection)
PC_URL = "http://192.168.137.1:8000/api/scan"

# --- TIMING (added for Test 2.4): one CSV row per scanned card -------------
TIMING_LOG = "/home/pi-model-b/scan_timing.csv"
TIMING_FIELDS = [
    "card_no", "mode", "time", "human_wait_s", "capture_s", "load_crop_s",
    "ocr_title_s", "ocr_footer_s", "ocr_s", "post_s", "total_s", "detections",
    "mem_avail_mb", "swap_used_mb",
]


def mem_status():
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


def log_timing_row(row):
    new_file = not os.path.exists(TIMING_LOG)
    with open(TIMING_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TIMING_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def print_timing_summary(rows):
    n = len(rows)
    if n == 0:
        return
    avg = lambda key: sum(r[key] for r in rows) / n
    busy = sum(r["total_s"] for r in rows)
    waits = sum(r["human_wait_s"] for r in rows if r["human_wait_s"] is not None)
    print("\n===== TIMING SUMMARY =====")
    print(f"mode                   : {'title + footer' if READ_FOOTER else 'title only'}")
    print(f"cards scanned          : {n}")
    print(f"avg capture            : {avg('capture_s'):.2f} s")
    print(f"avg load+crop          : {avg('load_crop_s'):.2f} s")
    print(f"avg OCR title band     : {avg('ocr_title_s'):.2f} s")
    print(f"avg OCR footer band    : {avg('ocr_footer_s'):.2f} s")
    print(f"avg OCR total          : {avg('ocr_s'):.2f} s")
    print(f"avg POST to PC         : {avg('post_s'):.2f} s")
    print(f"avg total per card     : {avg('total_s'):.2f} s  (Enter pressed -> PC replied)")
    print(f"cards/min, processing only        : {n / (busy / 60):.2f}")
    print(f"cards/min, incl. time between cards: {n / ((busy + waits) / 60):.2f}")
    print(f"log file: {TIMING_LOG}")


def send_scan_result(scan):
    payload = {
        "type": "card_scan",
        "detections": [
            {
                "text": detection["text"],
                "confidence": detection["confidence"],
                "box": detection["box"],
            }
            for detection in scan["detections"]
        ],
        "elapsed_seconds": round(scan["elapsed_seconds"], 2),
    }

    request = urllib.request.Request(
        PC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(f"PC replied: {response.read().decode()}")

    except OSError as error:
        print(f"Could not send results to PC: {error}")


def read_band(reader, image, band, crop_path):
    """OCR one horizontal band of the photo.

    Returns (detections, ocr_seconds, crop_seconds). Box y-coordinates are shifted
    by the band's top row, so every box is in FULL-PHOTO coordinates. The PC needs
    that to tell the footer (low in the photo) from the title (high in the photo).
    """
    t_crop = time.perf_counter()
    height = image.shape[0]
    y1 = max(0, min(height, int(round(height * band[0]))))
    y2 = max(0, min(height, int(round(height * band[1]))))
    crop = image[y1:y2, :]

    if crop.size == 0:
        raise ValueError(f"Crop is empty. Band {band} on a photo {height} px high")

    cv2.imwrite(crop_path, crop)
    crop_s = time.perf_counter() - t_crop

    print(f"Running OCR on rows {y1}-{y2}: {crop.shape[1]}x{crop.shape[0]}")

    t_ocr = time.perf_counter()
    result = reader.readtext(crop)
    ocr_s = time.perf_counter() - t_ocr

    detections = []
    for box, text, confidence in result:
        detections.append({
            "text": text,
            "confidence": float(confidence),
            "box": [[float(x), float(y) + y1] for x, y in box],
        })
    return detections, ocr_s, crop_s


def scan_card(reader, image_path=IMAGE_PATH, timings=None):
    t_load = time.perf_counter()
    image = cv2.imread(image_path)

    if image is None:
        raise FileNotFoundError(f"Could not load image: {image_path}")
    load_s = time.perf_counter() - t_load

    bands = [(TITLE_BAND, CROP_PATH)]
    if READ_FOOTER:
        bands.append((FOOTER_BAND, FOOTER_CROP_PATH))

    # Memory/swap just before OCR is the number that shows whether the Pi is swapping.
    if timings is not None:
        timings["mem_avail_mb"], timings["swap_used_mb"] = mem_status()

    detections = []
    ocr_times = []
    crop_total = 0.0
    for band, crop_path in bands:
        found, ocr_s, crop_s = read_band(reader, image, band, crop_path)
        detections.extend(found)
        ocr_times.append(ocr_s)
        crop_total += crop_s

    if timings is not None:
        timings["load_crop_s"] = load_s + crop_total
        timings["ocr_title_s"] = ocr_times[0]
        timings["ocr_footer_s"] = ocr_times[1] if len(ocr_times) > 1 else 0.0

    return {
        "image_path": image_path,
        "elapsed_seconds": sum(ocr_times),  # OCR only, all bands together
        "detections": detections,
    }


if __name__ == "__main__":
    t_model = time.perf_counter()
    print("Loading OCR model once...")
    reader = easyocr.Reader(["en"])
    print(f"OCR ready. (model load took {time.perf_counter() - t_model:.1f} s, paid once)")
    print(f"Mode: {'title + footer' if READ_FOOTER else 'title only'}")

    timing_rows = []
    last_done = None

    while True:
        command = input(
            "\nCapture/update magic.jpg, then press Enter to scan. "
            "Enter q to quit: "
        ).strip().lower()

        if command == "q":
            print("Exiting.")
            print_timing_summary(timing_rows)
            break

        t_enter = time.perf_counter()
        human_wait = (t_enter - last_done) if last_done is not None else None

        try:
            t_cap = time.perf_counter()
            subprocess.run(
            [
                "rpicam-still",
                "--timeout", "1000",
                "--width", str(CAPTURE_WIDTH),
                "--height", str(CAPTURE_HEIGHT),
                "--quality", "85",
                "--output", IMAGE_PATH,
                "--nopreview"
            ],
            check=True,
            )
            capture_s = time.perf_counter() - t_cap

            timings = {}
            scan = scan_card(reader, timings=timings)

            print(f"OCR time: {scan['elapsed_seconds']:.2f} seconds")

            for detection in scan["detections"]:
                print(
                    f"{detection['text']} "
                    f"({detection['confidence']:.1%})"
                )

            if not scan["detections"]:
                print("No text detected.")

            t_post = time.perf_counter()
            send_scan_result(scan)
            post_s = time.perf_counter() - t_post

            total_s = time.perf_counter() - t_enter
            last_done = time.perf_counter()

            row = {
                "card_no": len(timing_rows) + 1,
                "mode": "title+footer" if READ_FOOTER else "title",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "human_wait_s": None if human_wait is None else round(human_wait, 2),
                "capture_s": round(capture_s, 2),
                "load_crop_s": round(timings["load_crop_s"], 2),
                "ocr_title_s": round(timings["ocr_title_s"], 2),
                "ocr_footer_s": round(timings["ocr_footer_s"], 2),
                "ocr_s": round(scan["elapsed_seconds"], 2),
                "post_s": round(post_s, 2),
                "total_s": round(total_s, 2),
                "detections": len(scan["detections"]),
                "mem_avail_mb": None if timings["mem_avail_mb"] is None else round(timings["mem_avail_mb"]),
                "swap_used_mb": None if timings["swap_used_mb"] is None else round(timings["swap_used_mb"]),
            }
            timing_rows.append(row)
            log_timing_row(row)
            print(
                f"[timing] card {row['card_no']}: capture {row['capture_s']}s | "
                f"load+crop {row['load_crop_s']}s | OCR {row['ocr_s']}s "
                f"(title {row['ocr_title_s']}s, footer {row['ocr_footer_s']}s) | "
                f"POST {row['post_s']}s | TOTAL {row['total_s']}s | "
                f"swap used {row['swap_used_mb']} MB"
            )

        except (FileNotFoundError, ValueError) as error:
            print(f"Scan failed: {error}")
