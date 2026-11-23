# Pi feeder: servos that push cards in front of the scanner

Code in this folder runs on the **Raspberry Pi**. The PC web app never uses it; the PC only
receives the Pi's `POST /api/scan`, exactly as before.

```
feed (servos push the card in) -> settle -> capture -> OCR -> POST to PC -> clear (push it out) -> repeat
```

| File | What it is |
|---|---|
| `feeder.py` | The servo code. Real servos (`ContinuousServoMotor`) and pretend ones (`MockMotor`). |
| `autoscan.py` | The automatic loop: feed, photograph, OCR, send to the PC, log. |
| `servo_calibrate.py` | Bench tool to find each servo's true stop point. Run once per servo. |
| `feeder_config.example.json` | Pins, calibration and timing. Copy to `feeder_config.json` and edit. |
| `requirements-pi.txt` | Packages needed only for real servos. |

## Start here: run it with nothing wired (safe)

By default the servos are **mocks**: they print what they would do and move nothing.

```
cd ~/OCR-Card-Scanner            # wherever the repo is on the Pi
source ~/ocr-env/bin/activate
python -m pi.autoscan --cards 3
```
It prints lines like `[mock servo_a] run speed=0.30 forward`, takes a real photo, runs OCR, sends it to
the PC (the PC app must be running), and writes `~/autoscan_cycles.csv`. Use `--image ~/magic_small.jpg`
to reuse a saved photo instead of the camera. Use `--tally` to be asked "was exactly ONE card fed?"
after each cycle (an honest hand-checked count for the feeding test).

The loop stops after `--cards` cards, after `--max-empty` empty scans in a row (default 3: no cards
left, or a jam), or on Ctrl+C. The servos are always stopped at the end.

## Scan a stack from the browser (over campus Wi-Fi)

The campus network (PC on eduroam, Pi on Registered4OSU) blocks the Pi from reaching the PC
directly, but allows **SSH from the PC to the Pi**. So the PC opens one SSH session that carries a
reverse tunnel: on the Pi, `http://127.0.0.1:8000` reaches the app on the PC. The app stays on the
PC's `127.0.0.1`, so nobody else on campus can open it or start the motors.

On the PC, from the repo folder:
```
python -m uvicorn app.main:app --port 8000      # window 1: the app
.\scripts\pi_link.ps1                           # window 2: tunnel + scanner on the Pi (real servos)
.\scripts\pi_link.ps1 -Mock                     #   ...or with mock servos (nothing moves)
.\scripts\pi_link.ps1 -PiHost <new address>     #   ...if the Pi's address changed
```
Open http://127.0.0.1:8000/scan. When it says **Pi connected**, load the stack and click **Scan stack**.
The Pi then feeds, photographs, reads and ejects card after card on its own:
- Each card appears in the list; click its name to pick the printing and add it to the collection.
- A card is retried up to 3 times **without moving it** (new photo each time). If the PC still can't
  identify it, it is listed in red, ejected, and the run continues.
- No readable text after 3 tries = no card in front of the camera: the run ends ("stack is empty, or
  a card is jammed"). Nothing is ejected.
- **Stop** in the browser takes effect before the next card. Ctrl+C or closing the `pi_link` window
  stops the scanner and the servos.
- Each try is ~70 s of OCR, so the end-of-stack check takes about 3.5 minutes. Use
  `-AgentArgs "--attempts 2"` to shorten it.

**The Pi's address** (now 172.30.226.152) can change. On the Pi: `hostname -I`. Typing the SSH
password each time can be avoided by copying the PC's key once (PowerShell):
`type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh pi-model-b@<Pi address> "cat >> ~/.ssh/authorized_keys"`.

## One-card test (feed, scan, eject on Enter)

```
python -m pi.one_card --real-motor
```
1. Loads the OCR model first, then waits: **Enter** = servos A (GPIO18, header pin 12) and B (GPIO19,
   header pin 35) spin together and push the card in front of the camera.
2. Photo -> OCR (about a minute on the Pi 4) -> POST to the PC. The PC's `/scan` page refreshes every
   2 s, so the result appears in the browser by itself.
3. **Enter** again = both servos spin the same direction and push the card out the far side.

Uses `pi/feeder_config.two_servo.json` (hardware PWM via the `pwm-2chan` overlay, bench calibration
`stop_us` 1467 / `deadband_us` 28). Tune without editing files: `--speed`, `--feed-s`, `--settle-s`,
`--clear-s` (each run is still capped by `max_run_s` and `max_speed`). Without `--real-motor` the
servos are mocks. The photo goes to `~/one_card.jpg`, so `~/magic_small.jpg` is not overwritten.

## Motors, drivers, and what you have

A Raspberry Pi pin can supply only a few milliamps at 3.3 V. A motor needs far more, so the Pi never
powers a motor directly. What sits between them depends on the motor type:

| Motor type | Needs a driver board? | How the Pi controls it |
|---|---|---|
| **Hobby servo** (your FS90R) | **No.** The driver is built into the servo. | One signal wire carries a pulse; its width sets the speed and direction. |
| **Plain DC motor, 2 wires** (your R140, almost certainly) | **Yes**: a transistor/MOSFET (one direction) or an H-bridge board such as DRV8833 or TB6612 (both directions, speed by PWM) | Pins switch the driver; the driver switches the motor. |
| Stepper motor | Yes: a stepper driver such as A4988, DRV8825 or ULN2003 | Step and direction pins. |

**Current status: only the FS90R is supported.** The R140 is added later (see "Adding the R140" below).

The FS90R is a **continuous-rotation** servo: instead of moving to an angle, it spins, and the
pulse width sets the speed. About **1500 microseconds = stop**; shorter spins one way, longer the
other way; the farther from 1500, the faster. A small band around the stop point does nothing (the
dead band). Each servo's true stop point is slightly different, so each is calibrated.

**The FS90R** (FeeTech): 4.8-6 V supply, control range 700-2300 us, stop at 1500 us, adjustable with a
small screw on the servo.

## Adding the R140 (later)

An "R140" is normally a mini DC motor (RE-140 / RS-140 family). Datasheets differ by model: some are
**3-6 V**, others only **1.5-3 V and damaged above 3 V**. Read the markings on the motor body or the
seller's listing before powering it. Because it is a plain DC motor it needs a **driver**: a transistor
or MOSFET with a flyback diode (one direction), or an H-bridge board (DRV8833 / TB6612, which waste
little voltage on a small motor; an L298N drops about 2 V). Its speed is set by PWM duty cycle, not pulse
width, so it needs its own motor class (`gpiozero.Motor` or `PWMOutputDevice`) next to
`ContinuousServoMotor`; the rest of the code does not change.

## Wiring the FS90R (three wires)

Check the servo's own wire colors against its datasheet; they differ between brands (often brown/black
= ground, red = power, orange/yellow/white = signal).

1. **Power:** a **separate 5 V supply** for the servo (the FS90R takes 4.8-6 V), **not** the Pi's 5 V pin.
   A servo pushing a card can pull enough current to crash the Pi.
2. **Ground:** the servo supply's ground **and** a Pi ground pin must be connected together
   ("common ground"). Without it the signal has no reference and the servo twitches or ignores it.
3. **Signal:** each servo's signal wire to its own GPIO pin. The defaults in the code are **GPIO12 and
   GPIO13 (BCM numbers, not board pin numbers)**. These are **placeholders until you wire it**: set
   the real pins in `feeder_config.json`. Any free GPIO pin works; see "PWM for two servos" below.
4. Never connect a plain DC motor straight to a GPIO pin.

## PWM for two servos

A servo signal is a pulse repeated **50 times a second** (one every 20 ms). The pulse's width is the
command: 1500 us is stop, shorter or longer spins it one way or the other (1500 us is 7.5% of the 20 ms).

- **Two servos = two signal pins = two independent PWM signals.** Servo A's signal wire goes to one
  pin, servo B's to another. They share ground (and may share the 5 V supply), never a signal wire.
- **The code already does this:** `ContinuousServoMotor` creates one `gpiozero.Servo` per pin and sets
  its value (-1..1, mapped from the pulse width). Starting both together is just two `run()` calls
  back to back.
- **How steady are the pulses?** By default gpiozero (with the `lgpio` driver) times them in
  *software*. That is fine to start with. If a servo twitches or creeps at "stop", use steadier timing:
  - `pigpio` (DMA-timed, very steady, no code change): install and start the pigpio service, then run
    with `GPIOZERO_PIN_FACTORY=pigpio python -m pi.autoscan ... --real-motor`. Availability of the
    `pigpio` package on your Pi OS version is **not yet verified**.
  - True hardware PWM: GPIO12 (PWM0) and GPIO13 (PWM1) are the Pi's two hardware PWM channels; they
    need `dtoverlay=pwm-2chan` in `/boot/firmware/config.txt` and a different motor class. Not built;
    only worth it if the two options above are not steady enough.

## Bring-up checklist (do these in order, stop at the first thing that looks wrong)

1. **Mock loop** (above). Nothing is connected.
2. **Real camera, mock servos:** the same command. Confirm photo, OCR, and the result on the PC's
   `/scan` page. Cards must be fed by hand for now.
3. **Install the servo packages:** `pip install -r pi/requirements-pi.txt` (see the note in that file
   about the `ocr-env` virtual environment).
4. **Power the servos from the external supply with the signal wires NOT connected.** They must sit
   still. If one creeps, its stop screw needs trimming.
5. **Connect one servo's signal wire** and run the calibration tool with the card path clear and
   nothing for the servo to push:
   ```
   python -m pi.servo_calibrate --pin 12
   ```
   Follow the on-screen steps to find the stop pulse and dead band, then type `show` and copy the
   numbers into `feeder_config.json`. Repeat for the second servo on its pin.
6. **Direction:** the card must be pushed toward the camera. If the servo pushes the wrong way, set
   `"invert": true` for it in the config. Test at low speed for under a second.
7. **Both servos, low speed, short run, no card:** `python -m pi.autoscan --cards 1 --real-motor`.
8. **A real card.** Tune `feed_s` (how far it is pushed), `settle_s` (wait before the photo) and
   `clear_s` (eject push; `0` turns it off). Raise `speed` slowly; `max_speed` is a ceiling you
   raise on purpose.
9. Then `python -m pi.autoscan --cards 10 --real-motor`.

## Safety built in

- Every run stops the servos in a `finally`, so an error cannot leave them spinning.
- A single run longer than `max_run_s` (default 3 s) is refused; so is a speed above `max_speed`.
- Servos stop on Ctrl+C, on `kill` (SIGTERM) and on normal exit.
- Real servos are only used with `--real-motor`. Nothing sends a pulse until the code asks.
- To stop everything immediately, cut the **servo power supply**: it does not depend on the software.

## What the camera check can and cannot tell you

A cycle counts as "text read" when OCR found some text above a confidence floor. That proves a card
was in front of the camera, **not** that exactly one card arrived (two stuck together still read as
one). For an honest single-card count, run with `--tally`.

## The log (`~/autoscan_cycles.csv`)

One row per cycle: `feed_s`, `settle_s`, `capture_s`, `load_crop_s`, `ocr_title_s`, `ocr_footer_s`,
`ocr_s`, `post_s`, `clear_s`, `total_s`, `feed_ok`, `identified`, `exact`, `tally`, memory and swap.
`total_s` is the full hands-free time per card, so cards per minute comes straight from it.
