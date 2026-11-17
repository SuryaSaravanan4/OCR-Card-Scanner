"""Tests for pi/feeder.py and pi/autoscan.py's loop. No servos, camera, OCR model or
network involved: motors are MockMotors and time is a fake clock, so these run on any
machine and finish instantly.
"""
from __future__ import annotations

import json

import pytest

from pi.autoscan import has_text, run_cycles, summarize
from pi.feeder import (
    FeedConfig,
    Feeder,
    FeederError,
    FeederSettings,
    MockMotor,
    ServoCalibration,
    build_feeder,
    load_settings,
    settings_to_dict,
    speed_to_pulse_us,
)
from pi.servo_calibrate import suggest_calibration


class FakeClock:
    """Time that only moves when something sleeps, so tests don't actually wait."""

    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def make_feeder(config: FeedConfig | None = None, motors=2):
    clock = FakeClock()
    events: list = []
    mocks = [MockMotor(f"m{i}", events=events, verbose=False) for i in range(motors)]
    feeder = Feeder(mocks, config or FeedConfig(), sleep=clock.sleep, clock=clock.now)
    return feeder, mocks, events, clock


# ------------------------------------------------------------ pulse mapping


def test_speed_zero_is_exactly_the_stop_pulse():
    cal = ServoCalibration(stop_us=1520)
    assert speed_to_pulse_us(0, True, cal) == 1520
    assert speed_to_pulse_us(0, False, cal) == 1520


def test_forward_is_above_stop_and_reverse_below_and_both_clear_the_dead_band():
    cal = ServoCalibration(stop_us=1500, deadband_us=60)
    assert speed_to_pulse_us(0.01, True, cal) > 1500 + 60 - 1
    assert speed_to_pulse_us(0.01, False, cal) < 1500 - 60 + 1


def test_full_speed_reaches_the_limit_and_never_goes_past_it():
    cal = ServoCalibration()
    assert speed_to_pulse_us(1.0, True, cal) == cal.max_us
    assert speed_to_pulse_us(1.0, False, cal) == cal.min_us
    assert speed_to_pulse_us(5.0, True, cal) == cal.max_us  # over-range is clamped


def test_pulse_grows_with_speed():
    cal = ServoCalibration()
    pulses = [speed_to_pulse_us(s, True, cal) for s in (0.1, 0.3, 0.6, 1.0)]
    assert pulses == sorted(pulses) and len(set(pulses)) == 4


def test_invert_swaps_direction():
    normal = ServoCalibration()
    mirrored = ServoCalibration(invert=True)
    assert speed_to_pulse_us(0.3, True, normal) > normal.stop_us
    assert speed_to_pulse_us(0.3, True, mirrored) < mirrored.stop_us


def test_speed_scale_slows_a_faster_servo():
    fast = ServoCalibration(speed_scale=0.5)
    plain = ServoCalibration()
    assert speed_to_pulse_us(0.4, True, fast) < speed_to_pulse_us(0.4, True, plain)


def test_calibration_validation_rejects_nonsense():
    with pytest.raises(ValueError):
        ServoCalibration(stop_us=2400).validate()
    with pytest.raises(ValueError):
        ServoCalibration(deadband_us=-1).validate()
    ServoCalibration().validate()


def test_suggest_calibration_uses_the_larger_dead_band_side():
    cal = suggest_calibration(stop_us=1510, fwd_us=1565, rev_us=1470)
    assert cal["stop_us"] == 1510
    assert cal["deadband_us"] == 55  # max(1565-1510, 1510-1470=40)
    assert suggest_calibration(1500, None, None)["deadband_us"] == ServoCalibration().deadband_us


# ------------------------------------------------------------------- feeder


def test_advance_runs_both_motors_together_then_stops_both():
    feeder, mocks, events, clock = make_feeder()
    elapsed = feeder.advance(1.0)
    assert events == [
        ("m0", "run", 0.3, True), ("m1", "run", 0.3, True),
        ("m0", "stop"), ("m1", "stop"),
    ]
    assert clock.slept == [1.0]
    assert elapsed == 1.0
    assert not any(m.running for m in mocks)


def test_motors_stop_even_if_something_fails_mid_run():
    feeder, mocks, events, clock = make_feeder()

    def explode(seconds):
        raise RuntimeError("boom")

    feeder.sleep = explode
    with pytest.raises(RuntimeError):
        feeder.advance(1.0)
    assert not any(m.running for m in mocks)
    assert ("m0", "stop") in events and ("m1", "stop") in events


def test_a_motor_that_fails_to_start_still_leaves_everything_stopped():
    feeder, mocks, events, clock = make_feeder()

    def broken_run(speed, forward=True):
        raise OSError("pin busy")

    mocks[1].run = broken_run
    with pytest.raises(OSError):
        feeder.advance(1.0)
    assert not any(m.running for m in mocks)


def test_a_run_longer_than_max_run_s_is_refused_and_nothing_moves():
    feeder, mocks, events, clock = make_feeder(FeedConfig(feed_s=1.0, clear_s=1.0, max_run_s=2.0))
    with pytest.raises(FeederError):
        feeder.advance(2.5)
    assert events == [] and clock.slept == []


def test_a_speed_above_max_speed_is_refused():
    feeder, mocks, events, clock = make_feeder()
    with pytest.raises(FeederError):
        feeder.advance(0.5, speed=0.9)
    assert events == []


def test_zero_or_negative_duration_is_refused():
    feeder, *_ = make_feeder()
    with pytest.raises(FeederError):
        feeder.advance(0)


def test_stop_and_close_can_be_called_repeatedly():
    feeder, mocks, events, clock = make_feeder()
    feeder.stop()
    feeder.stop()
    feeder.close()
    feeder.close()
    assert not any(m.running for m in mocks)


def test_stop_keeps_stopping_the_others_if_one_motor_fails():
    feeder, mocks, events, clock = make_feeder()

    def bad_stop():
        raise OSError("stuck")

    mocks[0].stop = bad_stop
    with pytest.raises(FeederError):
        feeder.stop()
    assert ("m1", "stop") in events  # the second motor was still told to stop


def test_feed_cycle_pushes_then_settles():
    feeder, mocks, events, clock = make_feeder(FeedConfig(feed_s=1.2, settle_s=0.4))
    result = feeder.feed_cycle()
    assert clock.slept == [1.2, 0.4]
    assert result["feed_s"] == pytest.approx(1.2)
    assert result["settle_s"] == pytest.approx(0.4)


def test_clear_is_skipped_when_clear_s_is_zero():
    feeder, mocks, events, clock = make_feeder(FeedConfig(clear_s=0))
    assert feeder.clear() == 0.0
    assert events == []


def test_invalid_config_is_rejected():
    with pytest.raises(ValueError):
        FeedConfig(speed=0.9, max_speed=0.5).validate()
    with pytest.raises(ValueError):
        FeedConfig(feed_s=5, max_run_s=3).validate()


# ----------------------------------------------------------------- settings


def test_default_settings_build_mock_motors_that_move_nothing():
    feeder = build_feeder(FeederSettings(), real_motor=False)
    assert [m.name for m in feeder.motors] == ["servo_a", "servo_b"]
    assert all(isinstance(m, MockMotor) for m in feeder.motors)


def test_settings_round_trip_through_json(tmp_path):
    settings = FeederSettings()
    settings.calibrations[1].invert = True
    settings.feed.speed = 0.25
    path = tmp_path / "feeder.json"
    path.write_text(json.dumps(settings_to_dict(settings)), encoding="utf-8")
    loaded = load_settings(path)
    assert loaded.calibrations[1].invert is True
    assert loaded.feed.speed == 0.25
    assert loaded.pins == settings.pins


def test_missing_settings_file_gives_defaults(tmp_path):
    assert load_settings(tmp_path / "nope.json").pins == FeederSettings().pins
    assert load_settings(None).feed == FeedConfig()


def test_the_fs90r_only_config_is_valid_and_runs_one_motor_through_a_cycle():
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "pi" / "feeder_config.fs90r.json"
    settings = load_settings(path)
    settings.feed.validate()
    settings.calibrations[0].validate()
    assert (settings.names, settings.pins) == (["fs90r"], [12])

    clock = FakeClock()
    events: list = []
    feeder = build_feeder(settings, real_motor=False, sleep=clock.sleep, clock=clock.now, events=events)
    feeder.feed_cycle()
    feeder.clear()
    assert [e[:2] for e in events] == [("fs90r", "run"), ("fs90r", "stop"), ("fs90r", "run"), ("fs90r", "stop")]


def test_the_example_config_in_the_repo_is_valid():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "pi" / "feeder_config.example.json"
    settings = load_settings(example)
    settings.feed.validate()
    for cal in settings.calibrations:
        cal.validate()
    assert len(settings.pins) == len(settings.calibrations) == 2


# -------------------------------------------------------------- scan loop


def _scan(text="Sol Ring", conf=0.9):
    return {"detections": [{"text": text, "confidence": conf, "box": []}] if text else [], "ocr_s": 1.0}


def run(feeder, scans, cards=3, max_empty=2, replies=None, tally_fn=None):
    """Drive run_cycles with scripted scans/replies and a no-op camera."""
    scans = iter(scans)
    replies = iter(replies) if replies is not None else None
    captures = []
    rows_logged = []
    result = run_cycles(
        feeder,
        capture_fn=lambda: captures.append("shot"),
        scan_fn=lambda: next(scans),
        send_fn=lambda scan: next(replies) if replies is not None else {"exact": True, "candidates": 1},
        log_fn=rows_logged.append,
        cards=cards,
        max_empty=max_empty,
        tally_fn=tally_fn,
        clock=feeder.clock,
        say=lambda *_: None,
    )
    return result, captures, rows_logged


def test_cycle_order_is_feed_then_capture_then_clear():
    feeder, mocks, events, clock = make_feeder()
    order = []
    real_feed, real_clear = feeder.feed_cycle, feeder.clear
    feeder.feed_cycle = lambda: (order.append("feed"), real_feed())[1]
    feeder.clear = lambda: (order.append("clear"), real_clear())[1]

    run_cycles(
        feeder,
        capture_fn=lambda: order.append("capture"),
        scan_fn=lambda: (order.append("scan"), _scan())[1],
        send_fn=lambda s: (order.append("send"), {"exact": True})[1],
        log_fn=lambda r: order.append("log"),
        cards=1,
        clock=clock.now,
        say=lambda *_: None,
    )
    assert order == ["feed", "capture", "scan", "send", "clear", "log"]


def test_loop_stops_after_the_requested_number_of_cards():
    feeder, *_ = make_feeder()
    result, captures, rows = run(feeder, [_scan()] * 5, cards=3)
    assert result["stopped"] == "done"
    assert len(result["rows"]) == len(rows) == len(captures) == 3
    assert [r["cycle_no"] for r in rows] == [1, 2, 3]


def test_loop_stops_after_consecutive_empty_scans():
    feeder, *_ = make_feeder()
    scans = [_scan(), _scan(text=""), _scan(text="")]
    result, _, rows = run(feeder, scans, cards=10, max_empty=2)
    assert result["stopped"] == "no_cards"
    assert len(rows) == 3
    assert [r["feed_ok"] for r in rows] == [True, False, False]


def test_a_good_scan_resets_the_empty_counter():
    feeder, *_ = make_feeder()
    scans = [_scan(text=""), _scan(), _scan(text=""), _scan()]
    result, _, rows = run(feeder, scans, cards=4, max_empty=2)
    assert result["stopped"] == "done" and len(rows) == 4


def test_low_confidence_junk_does_not_count_as_a_fed_card():
    assert not has_text([{"text": "DL-", "confidence": 0.02}])
    assert not has_text([{"text": "   ", "confidence": 0.9}])
    assert has_text([{"text": "656", "confidence": 1.0}])


def test_identified_and_exact_come_from_the_pc_reply():
    feeder, *_ = make_feeder()
    replies = [{"exact": True, "candidates": 1}, {"exact": False, "candidates": 4}, {"exact": False, "candidates": 0}, None]
    _, _, rows = run(feeder, [_scan()] * 4, cards=4, replies=replies)
    assert [(r["identified"], r["exact"]) for r in rows] == [(True, True), (True, False), (False, False), (False, False)]


def test_servos_are_stopped_when_the_loop_is_interrupted():
    feeder, mocks, events, clock = make_feeder()

    def interrupt():
        raise KeyboardInterrupt

    result = run_cycles(
        feeder, capture_fn=interrupt, scan_fn=_scan, send_fn=lambda s: None,
        log_fn=lambda r: None, cards=3, clock=clock.now, say=lambda *_: None,
    )
    assert result["stopped"] == "interrupted"
    assert not any(m.running for m in mocks)


def test_servos_are_stopped_when_the_camera_fails():
    feeder, mocks, events, clock = make_feeder()

    def no_camera():
        raise OSError("no cameras available")

    with pytest.raises(OSError):
        run_cycles(feeder, capture_fn=no_camera, scan_fn=_scan, send_fn=lambda s: None,
                   log_fn=lambda r: None, cards=3, clock=clock.now, say=lambda *_: None)
    assert not any(m.running for m in mocks)


def test_missing_camera_command_gives_a_plain_message(monkeypatch):
    import subprocess

    from pi import autoscan

    def not_found(*a, **k):
        raise FileNotFoundError("rpicam-still")

    monkeypatch.setattr(autoscan.subprocess, "run", not_found)
    with pytest.raises(autoscan.CaptureError, match="--image"):
        autoscan.capture_photo()

    def failed(*a, **k):
        raise subprocess.CalledProcessError(255, "rpicam-still")

    monkeypatch.setattr(autoscan.subprocess, "run", failed)
    with pytest.raises(autoscan.CaptureError, match="list-cameras"):
        autoscan.capture_photo()


def test_tally_answers_are_recorded_and_summarised():
    feeder, *_ = make_feeder()
    answers = iter(["y", "y", "n"])
    _, _, rows = run(feeder, [_scan()] * 3, cards=3, tally_fn=lambda n: next(answers))
    s = summarize(rows)
    assert (s["tally_yes"], s["tally_total"]) == (2, 3)


def test_summary_counts_and_rate():
    feeder, *_ = make_feeder(FeedConfig(feed_s=1.0, settle_s=0.5, clear_s=1.0))
    _, _, rows = run(feeder, [_scan(), _scan(text=""), _scan()], cards=3, max_empty=5)
    s = summarize(rows)
    assert (s["cycles"], s["feed_ok"], s["identified"]) == (3, 2, 3)
    assert s["avg_total_s"] == pytest.approx(2.5)  # 1.0 feed + 0.5 settle + 1.0 clear on the fake clock
    assert s["cards_per_min"] == pytest.approx(60 / 2.5)
    assert summarize([]) == {"cycles": 0}
