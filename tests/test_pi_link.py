"""Tests for the browser <-> Pi control channel (app/pi_link.py) and the Scan page
routes that use it. No Pi, no network: the Pi's side is played by test requests and a
fake clock.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import pi_link, scans, service
from app.main import app


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(pi_link, "_clock", c)
    pi_link.clear()
    scans.clear()
    yield c
    pi_link.clear()
    scans.clear()


@pytest.fixture()
def client(db, clock):
    with TestClient(app) as c:
        yield c


def _hit(name="Sol Ring", id="abc"):
    return {"id": id, "name": name, "set_name": "Commander Legends", "set_code": "cmr",
            "collector_number": "472", "rarity": "uncommon", "image_uri": None, "price_usd": "1.00"}


def _det(text, x0, y0, x1, y1, confidence=0.9):
    return {"text": text, "confidence": confidence, "box": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


# ------------------------------------------------------------------ pi_link


def test_start_is_refused_until_the_pi_has_checked_in(clock):
    with pytest.raises(pi_link.PiNotConnected):
        pi_link.start_run()
    pi_link.poll(busy=False)
    assert pi_link.start_run()["state"] == "requested"


def test_the_pi_gets_start_once_then_idle_and_a_second_start_is_refused(clock):
    pi_link.poll(False)
    run = pi_link.start_run()
    assert pi_link.poll(False) == {"command": "start", "run_id": run["id"]}
    assert pi_link.poll(True) == {"command": "idle"}
    assert pi_link.state()["run"]["state"] == "running"
    with pytest.raises(pi_link.RunInProgress):
        pi_link.start_run()


def test_stop_reaches_the_pi_and_finished_ends_the_run(clock):
    pi_link.poll(False)
    run = pi_link.start_run()
    pi_link.poll(False)
    pi_link.stop_run()
    assert pi_link.poll(True) == {"command": "stop", "run_id": run["id"]}
    pi_link.report({"run_id": run["id"], "event": "finished", "reason": "stopped"})
    s = pi_link.state()["run"]
    assert (s["state"], s["active"], s["problem"]) == ("finished", False, False)


def test_stop_before_the_pi_picks_it_up_cancels_the_run(clock):
    pi_link.poll(False)
    pi_link.start_run()
    pi_link.stop_run()
    assert pi_link.poll(False) == {"command": "idle"}
    assert pi_link.state()["run"]["reason"] == "stopped"


def test_failures_are_collected_and_events_for_old_runs_ignored(clock):
    pi_link.poll(False)
    run = pi_link.start_run()
    pi_link.poll(False)
    pi_link.report({"run_id": run["id"], "event": "card_failed", "card_no": 2, "message": "could not identify"})
    pi_link.report({"run_id": run["id"] + 99, "event": "card_failed", "card_no": 5})
    pi_link.report({"run_id": run["id"], "event": "finished", "reason": "camera_error", "message": "boom"})
    s = pi_link.state()["run"]
    assert s["failures"] == [{"card_no": 2, "message": "could not identify"}]
    assert s["problem"] is True and "camera" in s["reason_text"]


def test_a_slow_card_is_not_a_lost_connection_but_ten_minutes_of_silence_is(clock):
    pi_link.poll(False)
    pi_link.start_run()
    pi_link.poll(False)
    clock.t += 200  # a card with retries, no check-in
    assert pi_link.state()["run"]["state"] == "running"
    clock.t += pi_link.RUN_TIMEOUT_S
    s = pi_link.state()
    assert s["run"]["reason"] == "lost_connection" and s["connected"] is False


def test_an_idle_pi_with_a_running_run_finishes_it(clock):
    pi_link.poll(False)
    pi_link.start_run()
    pi_link.poll(False)          # picked up
    pi_link.poll(False)          # ...but says it is idle (agent restarted / lost report)
    assert pi_link.state()["run"]["reason"] == "pi_idle"


def test_idle_pi_goes_disconnected_after_the_short_timeout(clock):
    pi_link.poll(False)
    clock.t += pi_link.PI_TIMEOUT_S + 1
    assert pi_link.state()["connected"] is False


# ------------------------------------------------------------------ routes


def test_scan_page_shows_not_connected_and_a_disabled_button(client):
    page = client.get("/scan").text
    assert "Pi not connected" in page
    assert "disabled" in page and "Scan stack" in page
    assert 'id="picker"' in page


def test_start_when_the_pi_is_away_shows_a_message(client):
    resp = client.post("/scan/start", headers={"HX-Request": "true"})
    assert "not connected" in resp.text


def test_full_run_through_the_routes(client, monkeypatch):
    monkeypatch.setattr(service, "search", lambda text, limit=10, set_code=None, collector_number=None:
                        [] if text == "Blurry" else [_hit(), _hit("Sol Ring", "def")])
    assert client.get("/api/pi/command?busy=0").json() == {"command": "idle"}
    page = client.post("/scan/start", headers={"HX-Request": "true"}).text
    assert "Stop" in page
    cmd = client.get("/api/pi/command?busy=0").json()
    assert cmd["command"] == "start"
    run_id = cmd["run_id"]

    # Card 1: identified by title. Card 2: unidentified on try 1, then replaced by try 2.
    base = {"detections": [_det("Sol Ring", 10, 20, 300, 60)], "run_id": run_id}
    client.post("/api/scan", json={**base, "card_no": 1, "attempt": 1})
    client.post("/api/scan", json={"detections": [_det("Blurry", 10, 20, 300, 60)],
                                   "run_id": run_id, "card_no": 2, "attempt": 1})
    client.post("/api/scan", json={"detections": [_det("Blurry", 10, 20, 300, 60)],
                                   "run_id": run_id, "card_no": 2, "attempt": 2})
    rows = scans.cards_for_run(run_id)
    assert [(r["card_no"], r["attempt"]) for r in rows] == [(1, 1), (2, 2)]

    client.post("/api/pi/event", json={"run_id": run_id, "event": "card_failed", "card_no": 2,
                                       "message": "could not identify the card after 3 tries"})
    client.post("/api/pi/event", json={"run_id": run_id, "event": "finished", "reason": "no_card"})

    page = client.get("/scan", headers={"HX-Request": "true"}).text
    assert "<nav" not in page
    assert "Sol Ring" in page and "2 printing(s)" in page
    assert "Card 2: could not identify the card after 3 tries" in page
    assert "couldn't identify a card" in page
    assert "stack is empty" in page
    assert page.index("Card 2") < page.index("Card 1")  # newest first

    picker = client.get(f"/scan/item/{rows[0]['id']}").text
    assert "Add to collection" in picker and "hx-post=\"/collection\"" in picker
    assert client.get("/scan/item/9999").status_code == 404


def test_add_from_the_picker_stays_on_the_page_but_a_plain_form_still_redirects(client, monkeypatch):
    added = []
    monkeypatch.setattr(service, "add_owned", lambda card_id, **kw: added.append(card_id))
    resp = client.post("/collection", data={"card_id": "abc"}, headers={"HX-Request": "true"})
    assert resp.status_code == 200 and "Added to collection" in resp.text
    resp = client.post("/collection", data={"card_id": "abc"}, follow_redirects=False)
    assert resp.status_code == 303
    assert added == ["abc", "abc"]


def test_posting_a_scan_counts_as_the_pi_checking_in(client, monkeypatch):
    monkeypatch.setattr(service, "search", lambda *a, **k: [])
    client.post("/api/scan", json={"detections": []})
    assert pi_link.state()["connected"] is True
