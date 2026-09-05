"""Ghost-player controller — ESP32-C3 / MicroPython, USB-serial revision
--------------------------------------------------------------------
Offline twin of main.py. The control layer below is identical, line for
line: same servo drivers, same pins, same angles, same calibration mode
on the BOOT button. Only the transport differs — there is no Wi-Fi AP,
no HTTP server, no WebSocket and no embedded browser UI. A master
program on a Linux host drives the rig over the native USB CDC port
instead (see control_host.py).

WHY USB IS THE FASTER LINK, NOT THE SLOWER ONE:
  The Wi-Fi build spends most of its latency budget on radio, not on
  code — association, retries, and the AP's own beacon interval. Native
  USB CDC on the C3 has none of that: it is a 12 Mbit/s wired pipe with
  no contention, and a control frame is a few dozen bytes. The limiting
  factor becomes the 50 Hz servo refresh again, exactly as it should be.

  It also removes the single-core scheduler pressure described in
  main.py: no TCP stack, no socket accept loop, no per-connection
  buffers competing with the servo coroutines for the same core.

PROTOCOL — newline-delimited JSON, one object per line, both directions.
  Chosen over a packed binary format deliberately: at these rates the
  parsing cost is irrelevant, and being able to drive the rig by typing
  a line into a serial terminal is worth far more during bring-up than
  the bytes it saves.

  HOST -> DEVICE, every key optional, unknown keys ignored:
      {"joyL": [x, y]}      x, y in -1.0 .. 1.0
      {"joyR": [x, y]}
      {"trigL": f}          0.0 (released) .. 1.0 (fully pulled)
      {"trigR": f}
      {"motorA": f}         0.0 .. 1.0 vibration intensity
      {"motorB": f}
      {"button": "A"|"B"|"X"|"Y"|null}      null == released
      {"arrow": "left"|"up"|"right"|"down"|null}
      {"ping": 1}           -> device replies {"ev": "pong"}
      {"get": 1}            -> device replies with a full state dump
      {"stop": 1}           -> zero every output immediately
      {"hb": 1}             -> heartbeat, no reply (see failsafe below)

  DEVICE -> HOST:
      {"ev": "ready"}                  once, at startup
      {"ev": "calib", "on": true}      BOOT button toggled calibration
      {"ev": "pong"}
      {"ev": "state", ...}             full state dump, answer to "get"
      {"ev": "failsafe"}               link went quiet, outputs zeroed
      {"ev": "err", "msg": "..."}      unparseable line

FAILSAFE:
  A servo rig that keeps a trigger pulled because the host crashed is a
  physical problem, not a software one. If nothing arrives from the host
  for LINK_TIMEOUT_MS, every output is zeroed. The host is expected to
  send {"hb":1} a few times a second when it has nothing else to say —
  cheap insurance, since idle periods are otherwise indistinguishable
  from a dead master.

RUNNING IT:
  boot.py imports `main`, so this file does not start on its own. Either
  point boot.py at `main_offline` instead, or from the REPL:
      >>> import main_offline
  Ctrl-C still interrupts: the protocol is plain ASCII JSON and never
  contains a 0x03 byte, so there is no reason to disable the interrupt.

For the full pin rationale, the LEDC-versus-RMT explanation and the
L298N wiring notes, see the docstring of main.py — all of it applies
here unchanged.
"""

import sys
import select
import time
import asyncio
import ujson
from machine import Pin, PWM
from esp32 import RMT


# ---------------------------------------------------------------------
# Servo driver (machine.PWM wrapper — 50 Hz, pulse width in microseconds)
# ---------------------------------------------------------------------
class Servo:
    def __init__(self, pin, freq=50, min_us=500, max_us=2500, angle_range=180):
        self._pwm = PWM(Pin(pin), freq=freq)
        self._min_us = min_us
        self._max_us = max_us
        self._angle_range = angle_range

    def move(self, angle):
        angle = max(0, min(self._angle_range, angle))
        us = self._min_us + (self._max_us - self._min_us) * angle / self._angle_range
        self._pwm.duty_ns(int(us * 1000))

    def deinit(self):
        self._pwm.deinit()


# ---------------------------------------------------------------------
# RMT servo driver — see main.py for why the triggers cannot use LEDC.
# ---------------------------------------------------------------------
class RMTServo:
    def __init__(self, channel, pin, home=0, min_us=500, max_us=2500,
                 angle_range=180, period_us=20000):
        self._rmt = RMT(channel, pin=Pin(pin), clock_div=80)
        self._min_us = min_us
        self._max_us = max_us
        self._angle_range = angle_range
        self._period_us = period_us
        self._last_us = None
        self._rmt.loop(True)
        self.move(home)

    def move(self, angle):
        angle = max(0, min(self._angle_range, angle))
        us = int(self._min_us + (self._max_us - self._min_us) * angle / self._angle_range)
        if us == self._last_us:
            return
        self._last_us = us
        self._rmt.write_pulses((us, self._period_us - us), 1)

    def deinit(self):
        self._rmt.loop(False)
        self._rmt.deinit()


# ---------------------------------------------------------------------
# Software PWM for the two L298N intensity pins.
# ---------------------------------------------------------------------
class SoftPWM:
    def __init__(self, pin, freq=200):
        self._pin = Pin(pin, Pin.OUT)
        self._pin.value(0)
        self._period = 1.0 / freq
        self.duty = 0.0

    async def run(self):
        while True:
            d = self.duty
            if d <= 0.0:
                self._pin.value(0)
                await asyncio.sleep(self._period)
            elif d >= 1.0:
                self._pin.value(1)
                await asyncio.sleep(self._period)
            else:
                self._pin.value(1)
                await asyncio.sleep(self._period * d)
                self._pin.value(0)
                await asyncio.sleep(self._period * (1.0 - d))


# ---------------------------------------------------------------------
# Hardware map
# ---------------------------------------------------------------------
colored = Servo(pin=21)
arrow_servo = Servo(pin=20)
servoxl = Servo(pin=0)
servoyl = Servo(pin=1)
servoxr = Servo(pin=2)
servoyr = Servo(pin=3)

# machine.bitstream (the neopixel timing generator) claims one RMT
# channel at startup, leaving only one of the C3's two TX channels free.
# Releasing it falls bitstream back to bit-banging, which costs nothing
# here since no addressable LEDs are driven.
RMT.bitstream_channel(None)

trigger_l = RMTServo(channel=0, pin=4, home=0)
trigger_r = RMTServo(channel=1, pin=10, home=180)

motorA_dir = Pin(6, Pin.OUT)   # IN2
motorA_dir.value(0)
motorA_en = SoftPWM(pin=5)     # IN1 — intensity

motorB_dir = Pin(8, Pin.OUT)   # IN4 — also the board's built-in LED
motorB_en = SoftPWM(pin=7)     # IN3


def status_led(on):
    # GPIO8 does double duty: it is IN4 on the driver and the SuperMini's
    # built-in LED, which is active LOW. Lighting it for calibration mode
    # therefore leaves IN4 HIGH for the whole of normal operation, which
    # reverses motor B and inverts what a duty on IN3 means. motor_b_task
    # compensates for the duty; the reversal is harmless on a vibration
    # motor, which buzzes the same either way.
    motorB_dir.value(0 if on else 1)


status_led(False)              # boot straight into the non-calibrating state
motorB_en.duty = 1.0           # IN4 is HIGH at rest; match it on IN3 to brake,
                               # or the motor runs flat out until motor_b_task
                               # gets its first cycle in.

boot_button = Pin(9, Pin.IN, Pin.PULL_UP)

centerxl, centeryl = 90, 90
centerxr, centeryr = 90, 90
radius = 9      # max servo travel, in degrees, at full joystick deflection
trigger_travel = 35  # degrees swept from released (0) to fully pulled (1.0)
trigger_l_rest = trigger_travel
trigger_r_rest = 180 - trigger_travel
S = 0.015       # internal control-loop interval, in seconds

buttons = {"A": 40, "B": 140, "Y": 90, "X": 180}
arrows = {"down": 40, "right": 140, "up": 90, "left": 180}

LINK_TIMEOUT_MS = 1500   # host silence after which outputs are zeroed
SERIAL_POLL = 0.005      # how often the RX task drains the USB buffer

# ---------------------------------------------------------------------
# Calibration mode — unchanged from main.py. A BOOT button press parks
# every servo here and holds it, so arms can be reseated without the
# control loops fighting the adjustment; a second press hands control
# back to the host.
# ---------------------------------------------------------------------
CALIBRATION = {
    "colored": 90,               # coloured-button servo, off every button
    "arrow": 90,                 # d-pad servo, off every one of the four directions
    "joyL_x": centerxl,          # sticks centred
    "joyL_y": centeryl,
    "joyR_x": centerxr,
    "joyR_y": centeryr,
    "trigL": trigger_l_rest + trigger_travel,   # triggers fully pulled
    "trigR": trigger_r_rest - trigger_travel,
}

SERVOS = {
    "colored": colored,
    "arrow": arrow_servo,
    "joyL_x": servoxl,
    "joyL_y": servoyl,
    "joyR_x": servoxr,
    "joyR_y": servoyr,
    "trigL": trigger_l,
    "trigR": trigger_r,
}

state = {
    "button": None,
    "arrow": None,
    "joyL": (0.0, 0.0),
    "joyR": (0.0, 0.0),
    "motorA": 0.0,
    "motorB": 0.0,
    "trigL": 0.0,
    "trigR": 0.0,
    "calibrating": False,
}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------
# Serial transport
# ---------------------------------------------------------------------
def send(obj):
    # print() is the whole TX path: MicroPython routes stdout straight to
    # the USB CDC endpoint, and it appends the newline the protocol uses
    # as its frame delimiter.
    print(ujson.dumps(obj))


def zero_outputs():
    state["button"] = None
    state["arrow"] = None
    state["joyL"] = (0.0, 0.0)
    state["joyR"] = (0.0, 0.0)
    state["motorA"] = 0.0
    state["motorB"] = 0.0
    state["trigL"] = 0.0
    state["trigR"] = 0.0


def apply_message(msg):
    for k in ("button", "arrow"):
        if k in msg:
            state[k] = msg[k]
    for k in ("joyL", "joyR"):
        if k in msg:
            x, y = msg[k]
            state[k] = (clamp(float(x), -1, 1), clamp(float(y), -1, 1))
    for k in ("motorA", "motorB", "trigL", "trigR"):
        if k in msg:
            state[k] = clamp(float(msg[k]), 0.0, 1.0)

    if msg.get("stop"):
        zero_outputs()
    if msg.get("ping"):
        send({"ev": "pong"})
    if msg.get("get"):
        send({
            "ev": "state",
            "button": state["button"],
            "arrow": state["arrow"],
            "joyL": list(state["joyL"]),
            "joyR": list(state["joyR"]),
            "motorA": state["motorA"],
            "motorB": state["motorB"],
            "trigL": state["trigL"],
            "trigR": state["trigR"],
            "calibrating": state["calibrating"],
        })


async def serial_rx_task():
    # Reads stdin a character at a time behind a poll() with a zero
    # timeout, so the coroutine never blocks the scheduler waiting on a
    # host that has nothing to say. asyncio.StreamReader is deliberately
    # not used: its behaviour over USB CDC varies between ports, whereas
    # poll() on sys.stdin is consistent.
    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)
    buf = ""
    last_rx = time.ticks_ms()
    link_up = False

    send({"ev": "ready"})

    while True:
        # Bounded per-pass drain: a host that floods the port must not be
        # able to starve the servo coroutines of scheduler time.
        for _ in range(256):
            if not poller.poll(0):
                break
            c = sys.stdin.read(1)
            if not c:
                break
            if c == "\n":
                line, buf = buf.strip(), ""
                last_rx = time.ticks_ms()
                link_up = True
                if line:
                    try:
                        apply_message(ujson.loads(line))
                    except (ValueError, TypeError, KeyError) as e:
                        send({"ev": "err", "msg": str(e)})
            elif c != "\r" and len(buf) < 512:
                buf += c
            elif len(buf) >= 512:
                buf = ""          # oversized garbage, resynchronise

        # Failsafe. Only armed once the host has spoken at least once, so
        # a board powered up with no master simply sits idle rather than
        # announcing a lost link it never had.
        if link_up and time.ticks_diff(time.ticks_ms(), last_rx) > LINK_TIMEOUT_MS:
            zero_outputs()
            link_up = False
            send({"ev": "failsafe"})

        await asyncio.sleep(SERIAL_POLL)


# ---------------------------------------------------------------------
# Control coroutines — identical to main.py
# ---------------------------------------------------------------------
async def button_task():
    # Edge-triggered: the servo only moves when state["button"] changes.
    # Press -> exact target angle, release -> a single nudge away from
    # the button. Re-applying every cycle would make the release nudge
    # accumulate instead of happening once.
    prev = "__unset__"
    current_angle = 90
    while True:
        if state["calibrating"]:
            prev = "__unset__"
            current_angle = CALIBRATION["colored"]
            await asyncio.sleep(S)
            continue
        b = state["button"]
        if b != prev:
            if b in buttons:
                current_angle = buttons[b]
            else:
                current_angle = current_angle + 25 if current_angle < 90 else current_angle - 25
            colored.move(current_angle)
            prev = b
        await asyncio.sleep(S)


async def arrow_task():
    prev = "__unset__"
    current_angle = 65
    while True:
        if state["calibrating"]:
            prev = "__unset__"
            current_angle = CALIBRATION["arrow"]
            await asyncio.sleep(S)
            continue
        a = state["arrow"]
        if a != prev:
            if a in arrows:
                current_angle = arrows[a]
            else:
                # 25 deg lands midway between two of the four positions
                # (40 -> 65, 90 -> 65, 140 -> 115, 180 -> 155) so no
                # neighbouring direction is pressed on the way out.
                current_angle = current_angle + 25 if current_angle < 90 else current_angle - 25
            arrow_servo.move(current_angle)
            prev = a
        await asyncio.sleep(S)


async def joystick_l_task():
    while True:
        if state["calibrating"]:
            await asyncio.sleep(S)
            continue
        x, y = state["joyL"]
        # Y is negated on both sticks, and X on the right stick only, to
        # match how the servo arms are mounted against the target sticks.
        servoxl.move(clamp(centerxl + x * radius, 0, 180))
        servoyl.move(clamp(centeryl - y * radius, 0, 180))
        await asyncio.sleep(S)


async def joystick_r_task():
    while True:
        if state["calibrating"]:
            await asyncio.sleep(S)
            continue
        x, y = state["joyR"]
        servoxr.move(clamp(centerxr - x * radius, 0, 180))
        servoyr.move(clamp(centeryr - y * radius, 0, 180))
        await asyncio.sleep(S)


async def trigger_task():
    while True:
        if state["calibrating"]:
            await asyncio.sleep(S)
            continue
        trigger_l.move(trigger_l_rest + state["trigL"] * trigger_travel)
        trigger_r.move(trigger_r_rest - state["trigR"] * trigger_travel)
        await asyncio.sleep(S)


async def motor_a_task():
    while True:
        motorA_en.duty = 0.0 if state["calibrating"] else state["motorA"]
        await asyncio.sleep(S)


async def motor_b_task():
    while True:
        # The L298N drives the motor whenever IN3 and IN4 differ. IN4 is
        # LOW only while calibrating (see status_led), so the duty that
        # means "still" flips with the mode. Both branches below hold
        # state["motorB"] to the same meaning: 0.0 still, 1.0 full.
        if state["calibrating"]:
            motorB_en.duty = 0.0
        else:
            motorB_en.duty = 1.0 - state["motorB"]
        await asyncio.sleep(S)


async def boot_button_task():
    # Toggles calibration mode, and re-asserts the table every cycle
    # while it is active: the control loops check the flag on their own
    # S interval, so one of them can still push a stale angle out just
    # after the mode changes. Continuously re-asserting wins that race.
    prev = 1                       # active low, released == 1
    while True:
        v = boot_button.value()
        if prev == 1 and v == 0:                  # falling edge = press
            state["calibrating"] = not state["calibrating"]
            status_led(state["calibrating"])
            send({"ev": "calib", "on": state["calibrating"]})
            await asyncio.sleep(0.2)              # ride out contact bounce
            v = boot_button.value()
        prev = v

        if state["calibrating"]:
            for name, angle in CALIBRATION.items():
                SERVOS[name].move(angle)

        await asyncio.sleep(0.02)


async def main():
    await asyncio.gather(
        serial_rx_task(),
        button_task(),
        arrow_task(),
        joystick_l_task(),
        joystick_r_task(),
        trigger_task(),
        motor_a_task(),
        motor_b_task(),
        boot_button_task(),
        motorA_en.run(),
        motorB_en.run(),
    )

asyncio.run(main())
