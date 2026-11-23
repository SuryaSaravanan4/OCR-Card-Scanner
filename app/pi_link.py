"""The browser <-> Raspberry Pi control channel: "Scan stack" and "Stop".

The Pi never waits for the PC to call it. A long-running agent on the Pi
(`pi/scan_agent.py`) asks `GET /api/pi/command` about once a second, and this
module answers: "start run N", "stop", or "idle". The Pi reports how a run went with
`POST /api/pi/event`. Results of each card still arrive through `/api/scan`
(see app/scans.py).

Run states: requested (the browser clicked, the Pi hasn't picked it up yet) ->
running -> finished (with a reason). Everything is in memory, like app/scans.py.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Callable

# The Pi counts as connected if it was heard from this recently. While idle it asks
# every second; during a run it only checks in between cards and when it posts a scan,
# and one card can take minutes (OCR ~70 s per try, up to 3 tries), hence the long
# allowance before a running run is declared lost.
PI_TIMEOUT_S = 10.0
RUN_TIMEOUT_S = 600.0

# What the page says for each way a run can end.
REASONS = {
    "no_card": "No card in front of the camera after several tries: the stack is empty, or a card is jammed.",
    "stopped": "Stopped.",
    "max_cards": "Stopped at the safety limit on cards per run.",
    "camera_error": "The camera failed.",
    "interrupted": "The scanner program on the Pi was stopped.",
    "lost_connection": "The Pi stopped responding during the run.",
    "pi_idle": "The Pi ended the run.",
    "error": "The Pi hit an error.",
}
# Endings that are not a problem; anything else is shown as an error.
NORMAL_ENDINGS = {"no_card", "stopped", "max_cards"}


class PiNotConnected(RuntimeError):
    pass


class RunInProgress(RuntimeError):
    pass


_lock = threading.Lock()
_clock: Callable[[], float] = time.monotonic
_last_seen: float | None = None
_run: dict | None = None
_run_counter = 0


def _connected_locked() -> bool:
    running = _run is not None and _run["state"] == "running"
    timeout = RUN_TIMEOUT_S if running else PI_TIMEOUT_S
    return _last_seen is not None and _clock() - _last_seen <= timeout


def seen() -> None:
    """The Pi was heard from some other way (it posted a scan)."""
    global _last_seen
    with _lock:
        _last_seen = _clock()


def _finish_locked(reason: str, message: str | None = None) -> None:
    if _run is not None and _run["state"] != "finished":
        _run["state"] = "finished"
        _run["reason"] = reason
        _run["message"] = message
        _run["stop_requested"] = False


def _check_lost_locked() -> None:
    if _run is not None and _run["state"] == "running" and not _connected_locked():
        _finish_locked("lost_connection")


def start_run() -> dict:
    """The browser's "Scan stack" click. Raises if the Pi isn't there or is busy."""
    global _run, _run_counter
    with _lock:
        _check_lost_locked()
        if not _connected_locked():
            raise PiNotConnected("The Pi is not connected. Start the scanner on the Pi first.")
        if _run is not None and _run["state"] in ("requested", "running"):
            raise RunInProgress("A scan is already running.")
        _run_counter += 1
        _run = {
            "id": _run_counter,
            "state": "requested",
            "started_at": datetime.now().strftime("%H:%M:%S"),
            "stop_requested": False,
            "reason": None,
            "message": None,
            "failures": [],
        }
        return dict(_run)


def stop_run() -> None:
    """The browser's "Stop" click. The Pi stops before feeding the next card."""
    with _lock:
        if _run is None:
            return
        if _run["state"] == "requested":
            _finish_locked("stopped")
        elif _run["state"] == "running":
            _run["stop_requested"] = True


def poll(busy: bool) -> dict:
    """The Pi asking what to do. `busy` = the Pi is in the middle of a run."""
    global _last_seen
    with _lock:
        _last_seen = _clock()
        if _run is None:
            return {"command": "idle"}
        if _run["state"] == "requested":
            _run["state"] = "running"
            return {"command": "start", "run_id": _run["id"]}
        if _run["state"] == "running":
            if not busy:
                # The Pi is idle but we think a run is going: its "finished" report was
                # lost, or the agent restarted. Don't leave the button stuck.
                _finish_locked("pi_idle")
                return {"command": "idle"}
            if _run["stop_requested"]:
                return {"command": "stop", "run_id": _run["id"]}
        return {"command": "idle"}


def report(event: dict) -> None:
    """An event from the Pi: card_failed, error, or finished."""
    with _lock:
        if _run is None or event.get("run_id") != _run["id"]:
            return  # about an old run; ignore
        kind = event.get("event")
        if kind == "card_failed":
            _run["failures"].append(
                {"card_no": event.get("card_no"), "message": event.get("message") or "could not identify"}
            )
        elif kind == "error":
            _run["failures"].append({"card_no": event.get("card_no"), "message": event.get("message") or "error"})
        elif kind == "finished":
            _finish_locked(event.get("reason") or "pi_idle", event.get("message"))


def state() -> dict:
    """What the Scan page shows: whether the Pi is connected and the current run."""
    with _lock:
        _check_lost_locked()
        run = None
        if _run is not None:
            run = {**_run, "failures": list(_run["failures"])}
            run["active"] = run["state"] in ("requested", "running")
            run["reason_text"] = REASONS.get(run["reason"], run["reason"]) if run["reason"] else None
            run["problem"] = bool(run["reason"]) and run["reason"] not in NORMAL_ENDINGS
        return {"connected": _connected_locked(), "run": run}


def clear() -> None:
    global _last_seen, _run, _run_counter
    with _lock:
        _last_seen = None
        _run = None
        _run_counter = 0
