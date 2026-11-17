"""Bench tool: find the true stop point, direction and dead band of ONE continuous servo.

Do this once per servo, with the card path clear and nothing for the servo to push.
Each pulse you send runs for only a short burst (default 0.7 s) and then the servo is
stopped, so it cannot run away if you step away.

    python -m pi.servo_calibrate --pin 12

Commands at the prompt (pulse widths are in microseconds; 1500 is nominally "stop"):
    p 1500     send that pulse for one burst and watch the servo
    + / -      step the last pulse up / down by --step (default 5 us) and send it
    stop       record the last pulse as this servo's STOP point (it did not move)
    fwd        record the last pulse as the first one that moved the servo one way
    rev        record the last pulse as the first one that moved it the other way
    show       print the calibration worked out so far
    q          quit

How to use it: (1) send 1500 and nudge up/down until the servo is perfectly still,
type `stop`; (2) step up from there until it just starts to turn, type `fwd`;
(3) step down from the stop point until it just turns the other way, type `rev`;
(4) type `show` and copy the numbers into pi/feeder_config.json.
"""
from __future__ import annotations

import argparse
import json
import time

from pi.feeder import SERVO_RANGE_US, ContinuousServoMotor, ServoCalibration


def suggest_calibration(stop_us: float, fwd_us: float | None, rev_us: float | None) -> dict:
    """Turn the three recorded pulses into calibration values.

    deadband_us is the distance from the stop point to the first pulse that moved the
    servo (the larger of the two sides, so the servo is always past the dead band).
    """
    offsets = []
    if fwd_us is not None:
        offsets.append(abs(fwd_us - stop_us))
    if rev_us is not None:
        offsets.append(abs(stop_us - rev_us))
    lo, hi = SERVO_RANGE_US
    return {
        "stop_us": stop_us,
        "min_us": lo,
        "max_us": hi,
        "deadband_us": max(offsets) if offsets else ServoCalibration().deadband_us,
        "speed_scale": 1.0,
        "invert": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Find a continuous servo's stop point and dead band.")
    parser.add_argument("--pin", type=int, required=True, help="BCM GPIO number the servo signal wire is on")
    parser.add_argument("--burst", type=float, default=0.7, help="seconds each pulse runs before stopping")
    parser.add_argument("--step", type=float, default=5.0, help="microseconds for + and -")
    args = parser.parse_args()

    lo, hi = SERVO_RANGE_US
    motor = ContinuousServoMotor("calibrate", args.pin, ServoCalibration())
    last = 1500.0
    stop_us = fwd_us = rev_us = None

    def burst(pulse: float) -> None:
        nonlocal last
        pulse = max(lo, min(hi, pulse))
        last = pulse
        print(f"  sending {pulse:.0f} us for {args.burst}s ...")
        motor._servo.value = motor._value(pulse)
        try:
            time.sleep(args.burst)
        finally:
            motor.stop()

    print(__doc__)
    try:
        while True:
            cmd = input(f"[{last:.0f} us] > ").strip().lower()
            if cmd in ("q", "quit"):
                break
            elif cmd.startswith("p "):
                try:
                    burst(float(cmd.split()[1]))
                except ValueError:
                    print("  usage: p 1500")
            elif cmd == "+":
                burst(last + args.step)
            elif cmd == "-":
                burst(last - args.step)
            elif cmd == "stop":
                stop_us = last
                print(f"  recorded STOP point {stop_us:.0f} us")
            elif cmd == "fwd":
                fwd_us = last
                print(f"  recorded first-moving pulse (one way) {fwd_us:.0f} us")
            elif cmd == "rev":
                rev_us = last
                print(f"  recorded first-moving pulse (other way) {rev_us:.0f} us")
            elif cmd == "show":
                if stop_us is None:
                    print("  record a stop point first (type: stop)")
                else:
                    print(json.dumps(suggest_calibration(stop_us, fwd_us, rev_us), indent=2))
            else:
                print("  commands: p <us>, +, -, stop, fwd, rev, show, q")
    finally:
        motor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
