"""Drive the servos that push a card in front of the camera.

Runs on the Raspberry Pi, but everything except `ContinuousServoMotor` also runs on a
PC: the real hardware class imports `gpiozero` only when it is created, and the tests
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
    """A real continuous-rotation servo on a GPIO pin, through gpiozero.

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


@dataclass
class FeedConfig:
    """Timing and speed of one feed cycle. All values are tuned on the bench."""

    speed: float = 0.3        # 0..1, how fast the servos push
    max_speed: float = 0.5    # ceiling: a faster request is refused (raise it on purpose)
    feed_s: float = 1.0       # servos run this long to bring the card in front of the camera
    settle_s: float = 0.5     # pause after the push so the card stops moving before the photo
    clear_s: float = 1.0      # servos run this long to push the card out (0 = no eject push)
    max_run_s: float = 3.0    # no single run may be longer than this

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
        self.sleep = sleep
        self.clock = clock
        self._closed = False

    def advance(self, seconds: float, speed: float | None = None, forward: bool = True) -> float:
        """Run every motor for `seconds`, then stop them. Returns the time actually spent."""
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
            for motor in self.motors:
                motor.run(speed, forward)
            self.sleep(seconds)
        finally:
            self.stop()
        return self.clock() - started

    def feed_cycle(self) -> dict:
        """Push the card in front of the camera, then wait for it to stop moving."""
        feed_s = self.advance(self.config.feed_s) if self.config.feed_s > 0 else 0.0
        t = self.clock()
        if self.config.settle_s > 0:
            self.sleep(self.config.settle_s)
        return {"feed_s": feed_s, "settle_s": self.clock() - t}

    def clear(self) -> float:
        """Push the scanned card out of the way. Does nothing if clear_s is 0."""
        return self.advance(self.config.clear_s) if self.config.clear_s > 0 else 0.0

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
        """Stop the motors on exit, Ctrl+C and SIGTERM."""
        atexit.register(self.close)

        def handler(signum, frame):
            self.stop()
            if signum == signal.SIGINT:
                raise KeyboardInterrupt
            raise SystemExit(128 + signum)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass  # not the main thread, or not supported here


# ---------------------------------------------------------------- settings file


@dataclass
class FeederSettings:
    """Which pins, which servos, and the feed timing. Loaded from a JSON file if given."""

    names: list[str] = field(default_factory=lambda: ["servo_a", "servo_b"])
    # PLACEHOLDER pins (BCM numbers, not board pin numbers). GPIO12 and GPIO13 are the Pi's two
    # hardware-PWM pins, but gpiozero's default driver (lgpio) times the pulses in software, so
    # they are NOT hardware-timed unless you switch drivers (see pi/README.md, "PWM for two
    # servos"). Any free GPIO pins work. Confirm against the real wiring before --real-motor.
    pins: list[int] = field(default_factory=lambda: [12, 13])
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


def build_feeder(
    settings: FeederSettings,
    real_motor: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.perf_counter,
    events: list | None = None,
) -> Feeder:
    """Make a Feeder. Without real_motor, every servo is a MockMotor that moves nothing."""
    motors: list[MotorBase] = []
    for name, pin, cal in zip(settings.names, settings.pins, settings.calibrations):
        if real_motor:
            motors.append(ContinuousServoMotor(name, pin, cal))
        else:
            motors.append(MockMotor(name, events=events))
    return Feeder(motors, settings.feed, sleep=sleep, clock=clock)
