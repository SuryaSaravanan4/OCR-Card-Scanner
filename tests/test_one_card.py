"""Tests for pi/one_card.py and the two-servo config. Mock motors and a fake clock: no
servos, camera, OCR model or network, so these run on any machine instantly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pi.feeder import FeedConfig, Feeder, FeederSettings, MockMotor, build_feeder, load_settings
from pi.one_card import DEFAULT_CONFIG, describe_reply, run_one_card

REPO = Path(__file__).resolve().parent.parent


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def make_feeder(config: FeedConfig | None = None):
    clock = FakeClock()
    events: list = []
    motors = [MockMotor(n, events=events, verbose=False) for n in ("servo_a", "servo_b")]
    feeder = Feeder(motors, config or FeedConfig(feed_s=1.5, settle_s=0.5, clear_s=2.0, speed=0.2),
                    sleep=clock.sleep, clock=clock.now)
    return feeder, motors, events, clock


def scan_with(text="Vampiric Tutor", conf=0.9):
    return {"detections": [{"text": text, "confidence": conf, "box": []}] if text else [], "ocr_s": 1.0}


def drive(feeder, order, scan=None, reply=None, wait=None):
    return run_one_card(
        feeder,
        capture_fn=lambda: order.append("capture"),
        scan_fn=lambda: (order.append("scan"), scan or scan_with())[1],
        send_fn=lambda s: (order.append("send"), reply if reply is not None else {"id": 1, "exact": True})[1],
        wait_fn=wait or (lambda prompt: order.append("wait")),
        say=lambda *_: None,
    )


def test_order_is_wait_feed_capture_scan_send_wait_eject():
    feeder, motors, events, clock = make_feeder()
    order: list = []
    real_feed, real_clear = feeder.feed_cycle, feeder.clear
    feeder.feed_cycle = lambda: (order.append("feed"), real_feed())[1]
    feeder.clear = lambda: (order.append("eject"), real_clear())[1]

    result = drive(feeder, order)
    assert order == ["wait", "feed", "capture", "scan", "send", "wait", "eject"]
    assert result["stage"] == "ejected" and result["feed_ok"] is True


def test_both_servos_start_together_and_eject_goes_the_same_direction_as_the_feed():
    feeder, motors, events, clock = make_feeder()
    drive(feeder, [])
    runs = [e for e in events if e[1] == "run"]
    assert runs == [
        ("servo_a", "run", 0.2, True), ("servo_b", "run", 0.2, True),   # feed
        ("servo_a", "run", 0.2, True), ("servo_b", "run", 0.2, True),   # eject, same direction
    ]
    assert clock.slept == [1.5, 0.5, 2.0]  # feed, settle, eject
    assert not any(m.running for m in motors)


def test_nothing_moves_before_the_first_enter():
    feeder, motors, events, clock = make_feeder()

    def first_wait_interrupted(prompt):
        raise KeyboardInterrupt

    result = drive(feeder, [], wait=first_wait_interrupted)
    assert [e for e in events if e[1] == "run"] == []
    assert result["stage"] == "start" and result.get("interrupted")


def test_ctrl_c_at_the_eject_prompt_leaves_the_servos_stopped_and_does_not_eject():
    feeder, motors, events, clock = make_feeder()
    prompts = iter([None, KeyboardInterrupt])

    def wait(prompt):
        step = next(prompts)
        if step is KeyboardInterrupt:
            raise KeyboardInterrupt

    result = drive(feeder, [], wait=wait)
    assert result["stage"] == "sent" and result.get("interrupted")
    assert len([e for e in events if e[1] == "run"]) == 2  # feed only, no eject
    assert not any(m.running for m in motors)


def test_eject_still_offered_when_ocr_reads_nothing_or_the_pc_is_unreachable():
    feeder, motors, events, clock = make_feeder()
    order: list = []
    result = run_one_card(
        feeder,
        capture_fn=lambda: None,
        scan_fn=lambda: scan_with(text=""),
        send_fn=lambda s: None,
        wait_fn=lambda p: order.append("wait"),
        say=lambda *_: None,
    )
    assert result["stage"] == "ejected"
    assert result["feed_ok"] is False and result["reply"] is None
    assert order == ["wait", "wait"]


def test_servos_stopped_if_the_camera_fails_after_the_feed():
    feeder, motors, events, clock = make_feeder()

    def no_camera():
        raise OSError("no cameras available")

    with pytest.raises(OSError):
        run_one_card(feeder, no_camera, scan_with, lambda s: None, lambda p: None, say=lambda *_: None)
    assert not any(m.running for m in motors)


def test_describe_reply():
    assert "NOT reached" in describe_reply(None)
    assert "exact" in describe_reply({"id": 3, "exact": True, "candidates": 1})
    assert "2 candidate" in describe_reply({"id": 3, "exact": False, "candidates": 2})
    assert "no match" in describe_reply({"id": 3, "exact": False, "candidates": 0})
    assert "offline" in describe_reply({"id": 3, "offline": True})


# ------------------------------------------------------------ the two-servo config


def test_two_servo_config_matches_the_wiring_and_bench_values():
    assert DEFAULT_CONFIG == REPO / "pi" / "feeder_config.two_servo.json"
    s = load_settings(DEFAULT_CONFIG)
    s.feed.validate()
    assert s.names == ["servo_a", "servo_b"]
    assert s.pins == [18, 19]  # header pins 12 and 35
    for cal in s.calibrations:
        cal.validate()
        # invert=True on both: the first real run spun the wrong way; the operator wants
        # counterclockwise. Both flipped, so they still turn the same way as each other.
        assert (cal.stop_us, cal.deadband_us, cal.invert) == (1467, 28, True)
    assert s.feed.speed == 0.2
    # Feed: A and B together. Eject: only B, the other way (the operator's spec, 2026-11-23).
    assert s.feed.feed_motors == {"servo_a": "forward", "servo_b": "forward"}
    assert s.feed.clear_motors == {"servo_b": "reverse"}


def test_with_the_real_config_the_eject_runs_only_servo_b_reversed():
    s = load_settings(DEFAULT_CONFIG)
    events: list = []
    feeder = build_feeder(s, real_motor=False, sleep=lambda x: None, events=events)
    for m in feeder.motors:
        m.verbose = False
    feeder.feed_cycle()
    feed_runs = [e for e in events if e[1] == "run"]
    events.clear()
    feeder.clear()
    eject_runs = [e for e in events if e[1] == "run"]
    assert feed_runs == [("servo_a", "run", 0.2, True), ("servo_b", "run", 0.2, True)]
    assert eject_runs == [("servo_b", "run", 0.2, False)]
    assert {e[0] for e in events if e[1] == "stop"} == {"servo_a", "servo_b"}  # both told to stop


def test_real_motor_refuses_pins_without_hardware_pwm_before_touching_anything():
    bad = FeederSettings(pins=[18, 35])
    with pytest.raises(ValueError, match="GPIO35"):
        build_feeder(bad, real_motor=True)
    shared = FeederSettings(pins=[12, 18])
    with pytest.raises(ValueError, match="share a PWM channel"):
        build_feeder(shared, real_motor=True)


def test_default_pins_are_the_wired_ones():
    assert FeederSettings().pins == [18, 19]
