"""Drive the servos that push a card in front of the camera.

Runs on the Raspberry Pi, but everything except the real motor classes also runs on a
PC: `ContinuousServoMotor` imports `gpiozero` only when it is created,
`HardwarePwmServoMotor` touches /sys/class/pwm only when it is created, and the tests
and "nothing wired" runs use `MockMotor`, which moves nothing.

How a continuous-rotation servo is controlled (FeeTech FS90R datasheet): not by
voltage but by the WIDTH of a pulse sent 50 times a second. About 1500 us means
"stop"; a shorter pulse spins one way, a longer pulse spins the other way, and the
farther from 1500 us, the faster. A small band around the stop point does nothing
(the "dead band"). Every servo's real stop point differs a little, so each one gets
its own `ServoCalibration`, found on the bench with `pi/servo_calibrate.py`.

Safety rules built in (do not remove):
  * every run is wrapped in try/finally so the servos are stopped even on an error;
  * a single run longer than `FeedConfig.max_run_s` is refused;
  * a speed above `FeedConfig.max_speed` is refused;
  * `stop()` can be called any number of times, and also runs on exit, Ctrl+C and
    SIGTERM once `install_safety_handlers()` has been called.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence

# The pulse range the gpiozero Servo object is built for. Its centre is 1500 us.
SERVO_RANGE_US = (700.0, 2300.0)


class FeederError(RuntimeError):
    """A feed command was refused or failed. The servos are stopped before this is raised."""


@dataclass
class ServoCalibration:
    """What one physical servo needs, found on the bench (see pi/servo_calibrate.py).

    stop_us      pulse width at which THIS servo does not move (nominally 1500)
    min_us/max_us  the pulse range allowed for this servo (never go outside it)
    deadband_us  how far from stop_us the pulse must be before the servo starts moving
    speed_scale  trims speed so two different servos push at the same pace
                 (a servo that spins faster than the other gets a value below 1.0)
    invert       True if this servo must turn the opposite way to push the card the
                 same direction (side-by-side servos are usually mirrored)
    """

    stop_us: float = 1500.0
    min_us: float = 700.0
    max_us: float = 2300.0
    deadband_us: float = 50.0
    speed_scale: float = 1.0
    invert: bool = False

    def validate(self) -> None:
        lo, hi = SERVO_RANGE_US
        if not (lo <= self.min_us < self.stop_us < self.max_us <= hi):
            raise ValueError(
                f"need {lo} <= min_us < stop_us < max_us <= {hi}, got "
                f"{self.min_us}, {self.stop_us}, {self.max_us}"
            )
        if self.deadband_us < 0:
            raise ValueError("deadband_us must be >= 0")
        if not (0 < self.speed_scale <= 2):
            raise ValueError("speed_scale must be between 0 (exclusive) and 2")


def speed_to_pulse_us(speed: float, forward: bool, cal: ServoCalibration) -> float:
    """Turn a speed (0 = stop .. 1 = full) into the pulse width to send, in microseconds.

    speed <= 0 gives exactly the servo's stop pulse. Otherwise the pulse starts just past
    the dead band and reaches the servo's limit at full speed, so even speed 0.01 really
    moves the servo. `forward` is relative to `cal.invert`.
    """
    if speed <= 0:
        return cal.stop_us
    speed = min(1.0, speed * cal.speed_scale)
    direction = forward != cal.invert
    room = (cal.max_us - cal.stop_us) if direction else (cal.stop_us - cal.min_us)
    offset = min(cal.deadband_us + speed * max(room - cal.deadband_us, 0.0), room)
    return cal.stop_us + offset if direction else cal.stop_us - offset


class MotorBase:
    """What the feeder needs from one motor."""

    name = "motor"

    def run(self, speed: float, forward: bool = True) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self.stop()


class MockMotor(MotorBase):
    """A pretend motor: records and prints what it was told, moves nothing.

    `events` can be a list shared by several motors so a test can check the order in
    which they were started and stopped.
    """

    def __init__(self, name: str = "mock", events: list | None = None, verbose: bool = True):
        self.name = name
        self.events = events if events is not None else []
        self.verbose = verbose
        self.running = False

    def run(self, speed: float, forward: bool = True) -> None:
        self.running = True
        self.events.append((self.name, "run", speed, forward))
        if self.verbose:
            print(f"  [mock {self.name}] run speed={speed:.2f} {'forward' if forward else 'reverse'}")

    def stop(self) -> None:
        self.running = False
        self.events.append((self.name, "stop"))
        if self.verbose:
            print(f"  [mock {self.name}] stop")


class ContinuousServoMotor(MotorBase):
    """A real continuous-rotation servo on a GPIO pin, through gpiozero (software-timed).

    Not used by `build_feeder` any more (it uses `HardwarePwmServoMotor`); kept for pins
    that have no hardware PWM.

    Wiring (three wires per servo): signal -> this GPIO pin, power -> a SEPARATE 5 V
    supply (not the Pi's 5 V pin), ground -> that supply's ground AND a Pi ground pin.
    """

    def __init__(
        self,
        name: str,
        pin: int,
        cal: ServoCalibration,
        detach_on_stop: bool = True,
        pin_factory=None,
    ):
        cal.validate()
        from gpiozero import Servo  # only here, so a PC without gpiozero still works

        self.name = name
        self.cal = cal
        self.detach_on_stop = detach_on_stop
        lo, hi = SERVO_RANGE_US
        kwargs = dict(
            initial_value=None,  # send NO pulses until we ask: nothing moves at start-up
            min_pulse_width=lo / 1_000_000,
            max_pulse_width=hi / 1_000_000,
            frame_width=0.020,
        )
        if pin_factory is not None:
            kwargs["pin_factory"] = pin_factory
        self._servo = Servo(pin, **kwargs)

    @staticmethod
    def _value(pulse_us: float) -> float:
        lo, hi = SERVO_RANGE_US
        mid, half = (lo + hi) / 2, (hi - lo) / 2
        return max(-1.0, min(1.0, (pulse_us - mid) / half))

    def run(self, speed: float, forward: bool = True) -> None:
        self._servo.value = self._value(speed_to_pulse_us(speed, forward, self.cal))

    def stop(self) -> None:
        self._servo.value = self._value(self.cal.stop_us)
        if self.detach_on_stop:
            time.sleep(0.04)  # let at least one stop pulse go out before pulses are switched off
            self._servo.detach()

    def close(self) -> None:
        self.stop()
        self._servo.close()


class HardwarePwmServoMotor(MotorBase):
    """A continuous-rotation servo driven by the Pi's hardware PWM through /sys/class/pwm.

    Needs the overlay in /boot/firmware/config.txt, e.g.
        dtoverlay=pwm-2chan,pin=18,func=2,pin2=19,func2=2
    which puts GPIO18 on channel 0 and GPIO19 on channel 1.
    Pulses come from the PWM peripheral, not the CPU, so there is no timing jitter.
    """

    PERIOD_NS = 20_000_000  # 20 ms frame = 50 Hz

    def __init__(
        self,
        name: str,
        channel: int,
        cal: ServoCalibration,
        chip: int = 0,
        detach_on_stop: bool = True,
        export_timeout: float = 2.0,
    ):
        cal.validate()
        self.name = name
        self.cal = cal
        self.detach_on_stop = detach_on_stop
        self._chip = Path(f"/sys/class/pwm/pwmchip{chip}")
        self._dir = self._chip / f"pwm{channel}"
        self._enabled = False

        if not self._chip.exists():
            raise RuntimeError(
                f"{self._chip} not found: is the pwm overlay in /boot/firmware/config.txt?"
            )
        if not self._dir.exists():
            (self._chip / "export").write_text(f"{channel}\n")

        # After export, the kernel creates pwmN and udev fixes its permissions a moment later.
        deadline = time.monotonic() + export_timeout
        while not os.access(self._dir / "duty_cycle", os.W_OK):
            if time.monotonic() > deadline:
                raise RuntimeError(f"{self._dir} is not writable (permissions or export failed)")
            time.sleep(0.05)

        self._write("enable", 0)
        self._write("period", self.PERIOD_NS)  # period first: duty may never exceed it
        self._write("duty_cycle", round(cal.stop_us * 1000))
        # Left disabled on purpose: no pulses until run() or stop() is called.

    def _write(self, attr: str, value: int) -> None:
        (self._dir / attr).write_text(f"{int(value)}\n")

    def _set_pulse_us(self, pulse_us: float) -> None:
        pulse_us = max(self.cal.min_us, min(self.cal.max_us, pulse_us))
        self._write("duty_cycle", round(pulse_us * 1000))  # sysfs wants nanoseconds
        if not self._enabled:
            self._write("enable", 1)
            self._enabled = True

    def run(self, speed: float, forward: bool = True) -> None:
        self._set_pulse_us(speed_to_pulse_us(speed, forward, self.cal))

    def stop(self) -> None:
        self._set_pulse_us(self.cal.stop_us)
        if self.detach_on_stop:
            time.sleep(0.04)  # let a stop pulse or two go out first
            self._write("enable", 0)
            self._enabled = False

    def close(self) -> None:
        try:
            self.stop()
        finally:
            self._write("enable", 0)
            self._enabled = False


@dataclass
class FeedConfig:
    """Timing and speed of one feed cycle. All values are tuned on the bench."""

    speed: float = 0.3        # 0..1, how fast the servos push
    max_speed: float = 0.5    # ceiling: a faster request is refused (raise it on purpose)
    feed_s: float = 1.0       # servos run this long to bring the card in front of the camera
    settle_s: float = 0.5     # pause after the push so the card stops moving before the photo
    clear_s: float = 1.0      # servos run this long to push the card out (0 = no eject push)
    max_run_s: float = 3.0    # no single run may be longer than this
    # Which servos run in each phase, and which way: {"servo_a": "forward", ...}.
    # None = every servo, forward (the original "all together" behaviour).
    # "forward"/"reverse" are relative to each servo's `invert` calibration.
    feed_motors: dict[str, str] | None = None
    clear_motors: dict[str, str] | None = None
    # A timed feed in place of the single push (feed_s / feed_motors). Each step starts
    # its servos together, then stops each one at its own time (seconds from the start
    # of the step), e.g.
    #   {"motors": {"servo_a": "forward", "servo_b": "forward"}, "speed": 0.1,
    #    "stop_at": {"servo_b": 0.8, "servo_a": 2.3}}
    # The steps run in order; settle_s is waited after the last one.
    feed_steps: list[dict] | None = None

    def validate(self) -> None:
        if not (0 < self.speed <= self.max_speed <= 1):
            raise ValueError("need 0 < speed <= max_speed <= 1")
        for name in ("feed_s", "settle_s", "clear_s"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.max_run_s <= 0:
            raise ValueError("max_run_s must be > 0")
        if self.feed_s > self.max_run_s or self.clear_s > self.max_run_s:
            raise ValueError("feed_s and clear_s may not exceed max_run_s")
        for name in ("feed_motors", "clear_motors"):
            spec = getattr(self, name)
            if spec is None:
                continue
            if not spec:
                raise ValueError(f"{name} must name at least one servo (or be left out for all)")
            bad = [d for d in spec.values() if d not in ("forward", "reverse")]
            if bad:
                raise ValueError(f"{name}: direction must be 'forward' or 'reverse', got {bad}")
        if self.feed_steps is not None:
            if not self.feed_steps:
                raise ValueError("feed_steps must have at least one step (or be left out)")
            for i, step in enumerate(self.feed_steps, start=1):
                self._validate_step(i, step)

    def _validate_step(self, i: int, step: dict) -> None:
        unknown_keys = sorted(set(step) - {"motors", "speed", "stop_at"})
        if unknown_keys:
            raise ValueError(f"feed step {i}: unknown setting(s) {unknown_keys}")
        motors = step.get("motors") or {}
        stop_at = step.get("stop_at") or {}
        if not motors:
            raise ValueError(f"feed step {i}: 'motors' must name at least one servo")
        bad = [d for d in motors.values() if d not in ("forward", "reverse")]
        if bad:
            raise ValueError(f"feed step {i}: direction must be 'forward' or 'reverse', got {bad}")
        if set(stop_at) != set(motors):
            raise ValueError(f"feed step {i}: 'stop_at' must give a stop time for exactly the servos in 'motors'")
        for name, t in stop_at.items():
            if not (0 < t <= self.max_run_s):
                raise ValueError(
                    f"feed step {i}: {name} stop time {t}s must be > 0 and <= max_run_s={self.max_run_s}"
                )
        speed = step.get("speed", self.speed)
        if not (0 < speed <= self.max_speed):
            raise ValueError(f"feed step {i}: speed {speed} must be > 0 and <= max_speed={self.max_speed}")


class Feeder:
    """Runs all the servos together: push the card in, wait, push it out."""

    def __init__(
        self,
        motors: Sequence[MotorBase],
        config: FeedConfig | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self.motors = list(motors)
        self.config = config or FeedConfig()
        self.config.validate()
        names = {m.name for m in self.motors}
        for spec_name in ("feed_motors", "clear_motors"):
            spec = getattr(self.config, spec_name) or {}
            unknown = sorted(set(spec) - names)
            if unknown:
                raise ValueError(f"{spec_name} names unknown servo(s) {unknown}; servos are {sorted(names)}")
        for i, step in enumerate(self.config.feed_steps or [], start=1):
            unknown = sorted(set(step["motors"]) - names)
            if unknown:
                raise ValueError(f"feed step {i} names unknown servo(s) {unknown}; servos are {sorted(names)}")
        self.sleep = sleep
        self.clock = clock
        self._closed = False

    def _plan(self, spec: dict[str, str] | None, forward: bool) -> list[tuple[MotorBase, bool]]:
        """Which motors to run and which way. No spec = every motor, `forward`."""
        if spec is None:
            return [(m, forward) for m in self.motors]
        return [(m, (spec[m.name] == "forward") == forward) for m in self.motors if m.name in spec]

    def advance(
        self,
        seconds: float,
        speed: float | None = None,
        forward: bool = True,
        motors: dict[str, str] | None = None,
    ) -> float:
        """Run the motors for `seconds`, then stop ALL of them. Returns the time spent.

        `motors` picks which ones run and their direction ({"servo_b": "reverse"});
        None runs every motor in `forward` direction.
        """
        speed = self.config.speed if speed is None else speed
        if seconds <= 0:
            raise FeederError(f"run time must be > 0 seconds, got {seconds}")
        if seconds > self.config.max_run_s:
            raise FeederError(
                f"refusing to run {seconds}s: longer than max_run_s={self.config.max_run_s}"
            )
        if not (0 < speed <= self.config.max_speed):
            raise FeederError(
                f"refusing speed {speed}: must be > 0 and <= max_speed={self.config.max_speed}"
            )
        started = self.clock()
        try:
            for motor, direction in self._plan(motors, forward):
                motor.run(speed, direction)
            self.sleep(seconds)
        finally:
            self.stop()
        return self.clock() - started

    def run_step(self, step: dict) -> list[tuple[str, float]]:
        """Run one timed step: start its servos together, then stop each at its own
        `stop_at` time (seconds from the step's start). Returns [(servo, stopped_at)] in
        stop order. Every servo is stopped before this returns or raises.
        """
        by_name = {m.name: m for m in self.motors}
        speed = step.get("speed", self.config.speed)
        stops = sorted(step["stop_at"].items(), key=lambda item: item[1])
        done: list[tuple[str, float]] = []
        started = self.clock()
        try:
            for name, direction in step["motors"].items():
                by_name[name].run(speed, direction == "forward")
            planned = 0.0
            for name, stop_at in stops:
                # Sleep by the plan, corrected for time already spent (stopping a servo
                # takes a few ms), so the last stop still lands on its planned time.
                wait = max(0.0, (stop_at - planned) - max(0.0, self.clock() - started - planned))
                self.sleep(wait)
                planned = stop_at
                by_name[name].stop()
                done.append((name, self.clock() - started))
        finally:
            self.stop()
        return done

    def feed_cycle(self) -> dict:
        """Push the card in front of the camera, then wait for it to stop moving.

        With `feed_steps` configured this runs the timed steps in order; otherwise the
        single push of `feed_s` seconds with `feed_motors`.
        """
        steps_done: list[list[tuple[str, float]]] = []
        t0 = self.clock()
        if self.config.feed_steps:
            for step in self.config.feed_steps:
                steps_done.append(self.run_step(step))
            feed_s = self.clock() - t0
        elif self.config.feed_s > 0:
            feed_s = self.advance(self.config.feed_s, motors=self.config.feed_motors)
        else:
            feed_s = 0.0
        t = self.clock()
        if self.config.settle_s > 0:
            self.sleep(self.config.settle_s)
        return {"feed_s": feed_s, "settle_s": self.clock() - t, "steps": steps_done}

    def clear(self) -> float:
        """Push the scanned card out of the way. Does nothing if clear_s is 0."""
        if self.config.clear_s <= 0:
            return 0.0
        return self.advance(self.config.clear_s, motors=self.config.clear_motors)

    def stop(self) -> None:
        """Stop every motor. Safe to call any number of times, from anywhere."""
        errors = []
        for motor in self.motors:
            try:
                motor.stop()
            except Exception as exc:  # keep stopping the others, then report
                errors.append(f"{motor.name}: {exc}")
        if errors:
            raise FeederError("could not stop: " + "; ".join(errors))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.stop()
        finally:
            for motor in self.motors:
                try:
                    motor.close()
                except Exception:
                    pass

    def install_safety_handlers(self) -> None:
        """Stop the motors on exit, Ctrl+C, SIGTERM and SIGHUP (the SSH session dropped)."""
        atexit.register(self.close)

        def handler(signum, frame):
            self.stop()
            if signum == signal.SIGINT:
                raise KeyboardInterrupt
            raise SystemExit(128 + signum)

        signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):  # not on Windows
            signals.append(signal.SIGHUP)
        for sig in signals:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass  # not the main thread, or not supported here


# ---------------------------------------------------------------- settings file


@dataclass
class FeederSettings:
    """Which pins, which servos, and the feed timing. Loaded from a JSON file if given."""

    names: list[str] = field(default_factory=lambda: ["servo_a", "servo_b"])
    # BCM numbers, not board pin numbers. As wired: servo A on GPIO18 (header pin 12),
    # servo B on GPIO19 (header pin 35). The pwm-2chan overlay in /boot/firmware/config.txt
    # routes exactly these two pins to hardware PWM channels 0 and 1.
    pins: list[int] = field(default_factory=lambda: [18, 19])
    calibrations: list[ServoCalibration] = field(
        default_factory=lambda: [ServoCalibration(), ServoCalibration()]
    )
    feed: FeedConfig = field(default_factory=FeedConfig)


def load_settings(path: str | Path | None) -> FeederSettings:
    """Read a settings JSON (see pi/feeder_config.example.json). No file means the defaults."""
    if path is None or not Path(path).exists():
        return FeederSettings()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    servos = raw.get("servos", [])
    return FeederSettings(
        names=[s["name"] for s in servos] or FeederSettings().names,
        pins=[int(s["pin"]) for s in servos] or FeederSettings().pins,
        calibrations=[ServoCalibration(**s.get("calibration", {})) for s in servos]
        or FeederSettings().calibrations,
        feed=FeedConfig(**raw.get("feed", {})),
    )


def settings_to_dict(settings: FeederSettings) -> dict:
    return {
        "servos": [
            {"name": n, "pin": p, "calibration": asdict(c)}
            for n, p, c in zip(settings.names, settings.pins, settings.calibrations)
        ],
        "feed": asdict(settings.feed),
    }


def describe_motion(spec: dict[str, str] | None) -> str:
    """'servo_a+servo_b forward' / 'servo_b reverse' / 'all servos' for the start-up line."""
    if not spec:
        return "all servos"
    return ", ".join(f"{name} {direction}" for name, direction in spec.items())


def describe_feed(feed: FeedConfig) -> str:
    """One line describing the feed, for the start-up print."""
    if not feed.feed_steps:
        return f"speed {feed.speed} | feed {feed.feed_s} s ({describe_motion(feed.feed_motors)})"
    parts = []
    for i, step in enumerate(feed.feed_steps, start=1):
        dirs = "/".join(sorted(set(step["motors"].values())))
        stops = ", ".join(f"{n} stops {t}s" for n, t in sorted(step["stop_at"].items(), key=lambda x: x[1]))
        parts.append(f"step {i}: {'+'.join(step['motors'])} {dirs} @{step.get('speed', feed.speed)} ({stops})")
    return " -> ".join(parts)


# BCM pin -> Pi 4 hardware PWM channel. 12 and 18 share channel 0; 13 and 19 share channel 1.
PWM_CHANNEL = {12: 0, 18: 0, 13: 1, 19: 1}


def build_feeder(
    settings: FeederSettings,
    real_motor: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.perf_counter,
    events: list | None = None,
) -> Feeder:
    """Make a Feeder. Without real_motor, every servo is a MockMotor that moves nothing."""
    if real_motor:
        channels = []
        for pin in settings.pins:
            if pin not in PWM_CHANNEL:
                raise ValueError(f"GPIO{pin} is not a hardware PWM pin; use one of {sorted(PWM_CHANNEL)}")
            channels.append(PWM_CHANNEL[pin])
        if len(set(channels)) != len(channels):
            raise ValueError(f"pins {settings.pins} share a PWM channel; use 12+13 or 18+19")

    motors: list[MotorBase] = []
    for name, pin, cal in zip(settings.names, settings.pins, settings.calibrations):
        if real_motor:
            motors.append(HardwarePwmServoMotor(name, PWM_CHANNEL[pin], cal))
        else:
            motors.append(MockMotor(name, events=events))
    return Feeder(motors, settings.feed, sleep=sleep, clock=clock)
