import cv2
import easyocr
import time
import subprocess
import json
import urllib.request


IMAGE_PATH = "/home/pi-model-b/magic_small.jpg"
CROP_PATH = "/home/pi-model-b/name_crop.jpg"

# Crop coordinates: y_start:y_end, x_start:x_end
Y_START = 0
Y_END = 540
X_START = 0
X_END = 1920

# PC receiver settings (home Ethernet connection)
PC_URL = "http://192.168.137.1:8000/api/scan"


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


def scan_card(reader, image_path=IMAGE_PATH):
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

    print(f"Running OCR on crop: {name_crop.shape[1]}x{name_crop.shape[0]}")

    start = time.time()
    result = reader.readtext(name_crop)
    elapsed = time.time() - start

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
    print("Loading OCR model once...")
    reader = easyocr.Reader(["en"])
    print("OCR ready.")

    while True:
        command = input(
            "\nCapture/update magic.jpg, then press Enter to scan. "
            "Enter q to quit: "
        ).strip().lower()

        if command == "q":
            print("Exiting.")
            break

        try:
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

            scan = scan_card(reader)

            print(f"OCR time: {scan['elapsed_seconds']:.2f} seconds")

            for detection in scan["detections"]:
                print(
                    f"{detection['text']} "
                    f"({detection['confidence']:.1%})"
                )

            if not scan["detections"]:
                print("No text detected.")

            send_scan_result(scan)

        except (FileNotFoundError, ValueError) as error:
            print(f"Scan failed: {error}")
