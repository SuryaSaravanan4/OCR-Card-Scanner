"""Tests for pi/scan_agent.py: the stack loop with retries, and the poll loop.
Mock servos, scripted camera/OCR/PC replies, no sleeping: runs anywhere, instantly.
"""
from __future__ import annotations

import pytest

from pi.autoscan import CaptureError
from pi.feeder import FeedConfig, Feeder, MockMotor
from pi.scan_agent import run_stack, serve


def make_feeder():
    events: list = []
    motors = [MockMotor(n, events=events, verbose=False) for n in ("servo_a", "servo_b")]
    feeder = Feeder(motors, FeedConfig(feed_s=1.5, settle_s=0.5, clear_s=2.0, speed=0.2),
                    sleep=lambda s: None, clock=lambda: 0.0)
    return feeder, motors


TEXT = {"detections": [{"text": "Sol Ring", "confidence": 0.9, "box": []}], "ocr_s": 1.0}
BLANK = {"detections": [], "ocr_s": 1.0}
HIT = {"id": 1, "exact": False, "candidates": 2, "offline": False}
MISS = {"id": 1, "exact": False, "candidates": 0, "offline": False}


class Script:
    """Plays the camera, OCR and PC: `scans` and `replies` are consumed in order."""

    def __init__(self, feeder, scans, replies=(), stop_after_checks=None, capture_errors=0):
        self.feeder = feeder
        self.scans = list(scans)
        self.replies = list(replies)
        self.capture_errors = capture_errors
        self.stop_after_checks = stop_after_checks
        self.checks = 0
        self.log: list = []
        self.events: list = []
        self.sent: list = []
        real_feed, real_clear = feeder.feed_cycle, feeder.clear
        feeder.feed_cycle = lambda: (self.log.append("feed"), real_feed())[1]
        feeder.clear = lambda: (self.log.append("eject"), real_clear())[1]

    def capture(self):
        self.log.append("photo")
        if self.capture_errors:
            self.capture_errors -= 1
            raise CaptureError("no cameras available")

    def scan(self):
        return self.scans.pop(0)

    def send(self, scan, extra):
        self.log.append("send")
        self.sent.append(extra)
        return self.replies.pop(0)

    def should_stop(self):
        self.checks += 1
        return self.stop_after_checks is not None and self.checks > self.stop_after_checks

    @property
    def problems(self):
        """Events other than the per-card progress pings."""
        return [e for e in self.events if e["event"] != "progress"]

    def run(self, **kw):
        kw.setdefault("attempts", 3)
        return run_stack(self.feeder, self.capture, self.scan, self.send, self.events.append,
                         self.should_stop, run_id=7, sleep=lambda s: None, say=lambda *_: None, **kw)


def test_two_good_cards_then_an_empty_tray_ends_the_run_without_ejecting():
    feeder, motors = make_feeder()
    s = Script(feeder, [TEXT, TEXT, BLANK, BLANK, BLANK], [HIT, HIT])
    result = s.run()
    assert result["reason"] == "no_card"
    assert (result["cards"], result["identified"], result["failed"]) == (2, 2, 0)
    assert s.log == ["feed", "photo", "send", "eject",
                     "feed", "photo", "send", "eject",
                     "feed", "photo", "photo", "photo"]  # 3 tries, card NOT moved, no eject
    assert [e["card_no"] for e in s.sent] == [1, 2]
    assert all(e["run_id"] == 7 for e in s.sent)
    assert not any(m.running for m in motors)


def test_an_unidentified_card_is_retried_in_place_then_reported_ejected_and_the_run_goes_on():
    feeder, _ = make_feeder()
    s = Script(feeder, [TEXT, TEXT, TEXT, TEXT, BLANK, BLANK, BLANK], [MISS, MISS, MISS, HIT])
    result = s.run()
    assert s.log[:8] == ["feed", "photo", "send", "photo", "send", "photo", "send", "eject"]
    assert [e["attempt"] for e in s.sent[:3]] == [1, 2, 3]
    assert s.problems == [{"event": "card_failed", "card_no": 1,
                           "message": "could not identify the card after 3 tries"}]
    assert (result["identified"], result["failed"], result["reason"]) == (1, 1, "no_card")


def test_a_retry_that_succeeds_stops_retrying():
    feeder, _ = make_feeder()
    s = Script(feeder, [TEXT, TEXT, BLANK, BLANK, BLANK], [MISS, HIT])
    result = s.run()
    assert s.problems == [] and result["identified"] == 1
    assert s.log[:6] == ["feed", "photo", "send", "photo", "send", "eject"]


def test_blank_reads_then_text_still_counts_as_a_card():
    feeder, _ = make_feeder()
    s = Script(feeder, [BLANK, TEXT, BLANK, BLANK, BLANK], [HIT])
    assert s.run()["identified"] == 1


def test_pc_unreachable_is_retried_then_ends_the_run():
    feeder, motors = make_feeder()
    s = Script(feeder, [TEXT], [None, None, None])
    result = s.run()
    assert result["reason"] == "pc_unreachable"
    assert s.log == ["feed", "photo", "send", "send", "send"]
    assert not any(m.running for m in motors)


def test_pc_offline_is_not_retried_and_the_card_is_reported():
    feeder, _ = make_feeder()
    s = Script(feeder, [TEXT, BLANK, BLANK, BLANK], [{**MISS, "offline": True}])
    s.run()
    assert s.log[:4] == ["feed", "photo", "send", "eject"]
    assert "offline" in s.problems[0]["message"]


def test_camera_failing_every_try_ends_the_run_and_reports_it():
    feeder, motors = make_feeder()
    s = Script(feeder, [], [], capture_errors=3)
    result = s.run()
    assert result["reason"] == "camera_error"
    assert s.problems[0]["event"] == "error" and "camera" in s.problems[0]["message"]
    assert "eject" not in s.log
    assert not any(m.running for m in motors)


def test_one_camera_hiccup_is_retried():
    feeder, _ = make_feeder()
    s = Script(feeder, [TEXT, BLANK, BLANK, BLANK], [HIT], capture_errors=1)
    assert s.run()["identified"] == 1


def test_stop_is_checked_before_each_new_card():
    feeder, _ = make_feeder()
    s = Script(feeder, [TEXT, TEXT], [HIT, HIT], stop_after_checks=1)
    result = s.run()
    assert result["reason"] == "stopped" and result["cards"] == 1
    assert s.log.count("feed") == 1 and s.log[-1] == "eject"


def test_the_requested_count_is_scanned_then_the_run_ends_done():
    feeder, _ = make_feeder()
    s = Script(feeder, [TEXT] * 5, [HIT] * 5)
    result = s.run(max_cards=2)
    assert (result["reason"], result["cards"], result["identified"]) == ("done", 2, 2)
    assert s.log.count("feed") == 2  # a third card is never moved


def test_progress_is_reported_before_each_card_is_fed():
    feeder, _ = make_feeder()
    s = Script(feeder, [TEXT] * 3, [HIT] * 3)
    s.run(max_cards=3)
    assert [e["card_no"] for e in s.events if e["event"] == "progress"] == [1, 2, 3]


def test_stack_running_out_before_the_count_ends_no_card_with_the_right_numbers():
    feeder, _ = make_feeder()
    s = Script(feeder, [TEXT, TEXT, BLANK, BLANK, BLANK], [HIT, HIT])
    result = s.run(max_cards=5)
    assert (result["reason"], result["cards"], result["identified"]) == ("no_card", 2, 2)


def test_with_no_eject_configured_the_next_card_waits_for_the_scan_and_nothing_ejects():
    events: list = []
    motors = [MockMotor(n, events=events, verbose=False) for n in ("servo_a", "servo_b")]
    steps = [{"motors": {"servo_a": "forward", "servo_b": "forward"}, "speed": 0.1,
              "stop_at": {"servo_b": 0.8, "servo_a": 2.3}}]
    feeder = Feeder(motors, FeedConfig(feed_s=0, settle_s=1.0, clear_s=0, speed=0.1, feed_steps=steps),
                    sleep=lambda s: None, clock=lambda: 0.0)
    s = Script(feeder, [TEXT, TEXT], [HIT, HIT])
    result = s.run(max_cards=2)
    assert result["reason"] == "done"
    assert s.log == ["feed", "photo", "send", "feed", "photo", "send"]


def test_ctrl_c_mid_run_stops_the_servos():
    feeder, motors = make_feeder()
    s = Script(feeder, [TEXT], [HIT])

    def interrupt():
        raise KeyboardInterrupt

    s.scan = interrupt
    assert s.run()["reason"] == "interrupted"
    assert not any(m.running for m in motors)


def test_an_unexpected_error_still_stops_the_servos():
    feeder, motors = make_feeder()
    s = Script(feeder, [], [])

    def broken():
        raise ValueError("bad image")

    s.scan = broken
    with pytest.raises(ValueError):
        s.run()
    assert not any(m.running for m in motors)


# ------------------------------------------------------------------ poll loop


def test_serve_runs_a_stack_when_told_and_reports_the_result():
    commands = iter([None, {"command": "idle"}, {"command": "start", "run_id": 4, "cards": 3},
                     {"command": "idle"}])
    started, events, said = [], [], []

    def start_run(run_id, cards):
        started.append((run_id, cards))
        return {"reason": "done", "cards": 3, "identified": 2, "failed": 1, "message": None}

    serve(lambda busy: next(commands), start_run, lambda e: events.append(e) or True,
          sleep=lambda s: None, say=said.append, max_polls=4)
    assert started == [(4, 3)]
    assert events == [{"run_id": 4, "event": "finished", "reason": "done", "message": None,
                       "cards": 3, "identified": 2, "failed": 1}]
    assert any("Cannot reach the PC" in m for m in said)
    assert any("Connected to the PC" in m for m in said)
