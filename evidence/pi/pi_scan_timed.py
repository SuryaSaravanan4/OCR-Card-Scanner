import cv2
import easyocr
import time
import subprocess
import json
import urllib.request
import csv
import os
from datetime import datetime


IMAGE_PATH = "/home/pi-model-b/magic_small.jpg"
CROP_PATH = "/home/pi-model-b/name_crop.jpg"

# Crop coordinates: y_start:y_end, x_start:x_end
Y_START = 0
Y_END = 540
X_START = 0
X_END = 1920

# PC receiver settings (home Ethernet connection)
PC_URL = "http://192.168.137.1:8000/api/scan"

# --- TIMING (added for Test 2.4): one CSV row per scanned card -------------
TIMING_LOG = "/home/pi-model-b/scan_timing.csv"
TIMING_FIELDS = [
    "card_no", "time", "human_wait_s", "capture_s", "load_crop_s", "ocr_s",
    "post_s", "total_s", "detections", "mem_avail_mb", "swap_used_mb",
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
    print(f"cards scanned          : {n}")
    print(f"avg capture            : {avg('capture_s'):.2f} s")
    print(f"avg load+crop          : {avg('load_crop_s'):.2f} s")
    print(f"avg OCR (readtext)     : {avg('ocr_s'):.2f} s")
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


def scan_card(reader, image_path=IMAGE_PATH, timings=None):
    t_load = time.perf_counter()
    image = cv2.imread(image_path)

    if image is None:
        raise FileNotFoundError(f"Could not load image: {image_path}")

    height, width = image.shape[:2]

    # Clamp the crop to the actual image dimensions.
    y1 = min(Y_START, height)
    y2 = min(Y_END, height)
    x1 = min(X_START, width)
    x2 = min(X_END, width)

    name_crop = image[y1:y2, x1:x2]

    if name_crop.size == 0:
        raise ValueError(
            f"Crop is empty. Image dimensions: {width}x{height}"
        )

    cv2.imwrite(CROP_PATH, name_crop)
    if timings is not None:
        timings["load_crop_s"] = time.perf_counter() - t_load

    print(f"Running OCR on crop: {name_crop.shape[1]}x{name_crop.shape[0]}")

    # Memory/swap just before OCR is the number that shows whether the Pi is swapping.
    if timings is not None:
        timings["mem_avail_mb"], timings["swap_used_mb"] = mem_status()

    start = time.perf_counter()
    result = reader.readtext(name_crop)
    elapsed = time.perf_counter() - start  # same meaning as before: OCR only

    detections = []
    for box, text, confidence in result:
        detections.append({
            "text": text,
            "confidence": float(confidence),
            "box": [[float(x), float(y)] for x, y in box],
        })

    return {
        "image_path": image_path,
        "elapsed_seconds": elapsed,
        "detections": detections,
    }


if __name__ == "__main__":
    t_model = time.perf_counter()
    print("Loading OCR model once...")
    reader = easyocr.Reader(["en"])
    print(f"OCR ready. (model load took {time.perf_counter() - t_model:.1f} s, paid once)")

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
                "--width", "1920",
                "--height", "1080",
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
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "human_wait_s": None if human_wait is None else round(human_wait, 2),
                "capture_s": round(capture_s, 2),
                "load_crop_s": round(timings["load_crop_s"], 2),
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
                f"load+crop {row['load_crop_s']}s | OCR {row['ocr_s']}s | "
                f"POST {row['post_s']}s | TOTAL {row['total_s']}s | "
                f"swap used {row['swap_used_mb']} MB"
            )

        except (FileNotFoundError, ValueError) as error:
            print(f"Scan failed: {error}")
