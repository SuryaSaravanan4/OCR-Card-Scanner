"""Tests for the counted stack scan: the timed servo steps (pi/feeder.py), the rotated
crop boxes (pi/autoscan.py), and the card count + end-of-run heads-up in the browser
(app/pi_link.py, /scan routes). No servos, camera, OCR model or network involved.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import pi_link, scans
from app.main import app
from pi.autoscan import FOOTER_BOX, TITLE_BOX, crop_box, read_box
from pi.feeder import (
    FeedConfig,
    Feeder,
    MockMotor,
    ServoCalibration,
    build_feeder,
    load_settings,
    speed_to_pulse_us,
)

STACK_CONFIG = Path(__file__).resolve().parent.parent / "pi" / "feeder_config.stack.json"


class FakeClock:
    """Time that only moves when something sleeps."""

    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def stack_feeder(stop_cost: float = 0.0):
    """The real stack config, with mock motors on a fake clock that logs (time, event).
    `stop_cost` makes each stop take that long, like the real servo's 40 ms detach."""
    settings = load_settings(STACK_CONFIG)
    clock = FakeClock()
    log: list = []

    class Logged(MockMotor):
        def run(self, speed, forward=True):
            super().run(speed, forward)
            log.append((round(clock.t, 3), self.name, "run", speed, forward))

        def stop(self):
            if self.running:
                log.append((round(clock.t, 3), self.name, "stop"))
                clock.t += stop_cost
            super().stop()

    motors = [Logged(n, verbose=False) for n in settings.names]
    feeder = Feeder(motors, settings.feed, sleep=clock.sleep, clock=clock.now)
    return feeder, motors, log, clock


# ------------------------------------------------------------- timed servo steps


def test_the_stack_config_runs_the_bench_sequence_exactly():
    feeder, motors, log, clock = stack_feeder()
    result = feeder.feed_cycle()
    assert log == [
        (0.0, "servo_a", "run", 0.1, True), (0.0, "servo_b", "run", 0.1, True),
        (0.8, "servo_b", "stop"),
        (2.3, "servo_a", "stop"),
        (2.3, "servo_a", "run", 0.1, False), (2.3, "servo_b", "run", 0.1, False),
        (2.8, "servo_b", "stop"),
        (3.3, "servo_a", "stop"),
    ]
    assert clock.t == pytest.approx(4.3)  # + the 1.0 s gap before the photo
    assert result["feed_s"] == pytest.approx(3.3) and result["settle_s"] == pytest.approx(1.0)
    assert not any(m.running for m in motors)


def test_the_stack_config_matches_the_bench_script_pulses():
    # The bench script used ServoCalibration(stop_us=1467, deadband_us=28): invert False,
    # so the same speed and direction must give the same pulse width.
    settings = load_settings(STACK_CONFIG)
    bench = ServoCalibration(stop_us=1467, deadband_us=28)
    for cal in settings.calibrations:
        for forward in (True, False):
            assert speed_to_pulse_us(0.1, forward, cal) == speed_to_pulse_us(0.1, forward, bench)
    assert settings.feed.clear_s == 0  # no eject step


def test_a_slow_stop_does_not_push_the_later_stops_late():
    feeder, _, log, _ = stack_feeder(stop_cost=0.04)
    feeder.run_step(feeder.config.feed_steps[0])
    stops = {name: t for t, name, kind, *_ in log if kind == "stop"}
    assert stops["servo_b"] == pytest.approx(0.8)
    assert stops["servo_a"] == pytest.approx(2.3)


def test_servos_stop_if_something_fails_mid_step():
    feeder, motors, _, _ = stack_feeder()

    def boom(seconds):
        raise RuntimeError("power glitch")

    feeder.sleep = boom
    with pytest.raises(RuntimeError):
        feeder.feed_cycle()
    assert not any(m.running for m in motors)


@pytest.mark.parametrize("step, message", [
    ({"motors": {"servo_a": "forward"}, "stop_at": {"servo_a": 5.0}}, "max_run_s"),
    ({"motors": {"servo_a": "forward"}, "speed": 0.9, "stop_at": {"servo_a": 1.0}}, "max_speed"),
    ({"motors": {"servo_a": "sideways"}, "stop_at": {"servo_a": 1.0}}, "direction"),
    ({"motors": {"servo_a": "forward"}, "stop_at": {"servo_b": 1.0}}, "stop_at"),
    ({"motors": {}, "stop_at": {}}, "at least one"),
])
def test_bad_steps_are_refused(step, message):
    with pytest.raises(ValueError, match=message):
        FeedConfig(feed_steps=[step]).validate()


def test_a_step_naming_an_unknown_servo_is_refused():
    config = FeedConfig(feed_steps=[{"motors": {"servo_z": "forward"}, "stop_at": {"servo_z": 1.0}}])
    with pytest.raises(ValueError, match="unknown servo"):
        Feeder([MockMotor("servo_a", verbose=False)], config)


def test_the_stack_config_builds_with_mock_motors():
    feeder = build_feeder(load_settings(STACK_CONFIG), real_motor=False)
    assert [m.name for m in feeder.motors] == ["servo_a", "servo_b"]


# ------------------------------------------------------------- crop boxes


class FakeReader:
    """Returns one detection near the top-left of whatever crop it is given."""

    def readtext(self, crop):
        return [([[1, 2], [11, 2], [11, 6], [1, 6]], "text", 0.9)]


ROTATED = (1920, 1080)  # a 1920x1080 photo after rotating 90 degrees: 1920 high, 1080 wide


def test_crop_box_cuts_fractions_of_the_rotated_image():
    image = np.zeros((*ROTATED, 3), dtype=np.uint8)
    crop, dx, dy = crop_box(image, TITLE_BOX)
    assert (dx, dy) == (int(0.27 * 1080), int(0.28 * 1920))
    assert crop.shape[:2] == (int(0.37 * 1920) - dy, int(0.79 * 1080) - dx)


def test_read_box_shifts_boxes_back_so_the_footer_sits_below_the_title():
    image = np.zeros((*ROTATED, 3), dtype=np.uint8)
    written: list = []

    def write(path, crop):
        written.append(path)

    title, *_ = read_box(FakeReader(), image, TITLE_BOX, Path("t.jpg"), "title", write=write)
    footer, *_ = read_box(FakeReader(), image, FOOTER_BOX, Path("f.jpg"), "footer", write=write)
    assert title[0]["region"] == "title" and footer[0]["region"] == "footer"
    assert title[0]["box"][0] == [1 + int(0.27 * 1080), 2 + int(0.28 * 1920)]
    assert footer[0]["box"][0] == [1 + int(0.29 * 1080), 2 + int(0.63 * 1920)]
    assert min(y for _, y in footer[0]["box"]) > max(y for _, y in title[0]["box"])
    assert written == [str(Path("t.jpg")), str(Path("f.jpg"))]


def test_an_empty_crop_is_a_plain_error():
    with pytest.raises(ValueError, match="footer crop is empty"):
        read_box(FakeReader(), np.zeros((2, 2, 3), dtype=np.uint8), FOOTER_BOX, Path("f.jpg"),
                 "footer", write=lambda p, c: None)


# ------------------------------------------------------------- count + heads-up


@pytest.fixture()
def fresh(monkeypatch):
    monkeypatch.setattr(pi_link, "_clock", lambda: 1000.0)
    pi_link.clear()
    scans.clear()
    yield
    pi_link.clear()
    scans.clear()


@pytest.fixture()
def client(db, fresh):
    with TestClient(app) as c:
        yield c


def _finish(run_id, reason, cards, identified, failed, message=None):
    pi_link.report({"run_id": run_id, "event": "finished", "reason": reason, "message": message,
                    "cards": cards, "identified": identified, "failed": failed})


@pytest.mark.parametrize("bad", [0, -1, 61])
def test_out_of_range_counts_are_refused(fresh, bad):
    pi_link.poll(False)
    with pytest.raises(ValueError, match="1 to 60"):
        pi_link.start_run(bad)


def test_all_cards_scanned_is_a_green_summary(fresh):
    pi_link.poll(False)
    run = pi_link.start_run(15)
    assert pi_link.poll(False)["cards"] == 15
    _finish(run["id"], "done", 15, 15, 0)
    s = pi_link.state()["run"]
    assert s["summary"]["headline"] == "Scanned all 15 cards. 15 identified."
    assert s["problem"] is False


def test_unidentified_cards_make_the_summary_a_warning(fresh):
    pi_link.poll(False)
    run = pi_link.start_run(5)
    pi_link.poll(False)
    _finish(run["id"], "done", 5, 3, 2)
    s = pi_link.state()["run"]
    assert "3 identified, 2 could not be identified" in s["summary"]["headline"]
    assert s["problem"] is True


def test_stopping_early_lists_the_cards_not_scanned(fresh):
    pi_link.poll(False)
    run = pi_link.start_run(15)
    pi_link.poll(False)
    _finish(run["id"], "camera_error", 9, 9, 0, message="no cameras available")
    summary = pi_link.state()["run"]["summary"]
    assert summary["headline"] == "Only 9 of 15 cards were scanned. 9 identified."
    assert summary["lines"][0] == "Not scanned: cards 10-15."
    assert "camera failed" in summary["lines"][1] and "no cameras available" in summary["lines"][1]


def test_a_lost_pi_uses_the_last_progress_card_for_the_count(fresh, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(pi_link, "_clock", lambda: t[0])
    pi_link.poll(False)
    run = pi_link.start_run(4)
    pi_link.poll(False)
    pi_link.report({"run_id": run["id"], "event": "progress", "card_no": 2})
    t[0] += pi_link.RUN_TIMEOUT_S + 1
    summary = pi_link.state()["run"]["summary"]
    assert summary["fed"] == 2 and "Not scanned: cards 3-4." in summary["lines"]
    assert "stopped responding" in summary["lines"][1]


@pytest.mark.parametrize("value", ["", "abc", "0", "999"])
def test_a_bad_count_in_the_browser_shows_a_message_and_starts_nothing(client, value):
    client.get("/api/pi/command?busy=0")
    page = client.post("/scan/start", data={"cards": value}, headers={"HX-Request": "true"}).text
    assert "Enter how many cards to scan: 1 to 60." in page
    assert pi_link.state()["run"] is None


def test_the_count_box_sits_outside_the_refreshing_panel(client):
    page = client.get("/scan").text
    assert page.index('id="scan-count"') < page.index('id="scan-panel"')
    assert 'hx-include="#scan-count"' in page
