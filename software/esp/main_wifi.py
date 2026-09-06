"""Ghost-player controller — ESP32-C3 / MicroPython (low-latency revision)
--------------------------------------------------------------------
Change from previous version: the browser-to-ESP32 control channel now
uses a persistent WebSocket instead of one HTTP POST per input event.

Why this matters for latency:
  - Each `fetch()` POST opened a *new* TCP connection: SYN / SYN-ACK / ACK
    handshake, then request, then response, then teardown — several
    round trips per single joystick sample.
  - A WebSocket performs that handshake exactly once at page load; every
    subsequent input is a single small frame over the already-open
    socket, with no reconnection cost.
  - MicroPython's asyncio scheduler is cooperative and single-core on
    the C3: a burst of simultaneous fetch() connections queues behind
    each other. A steady stream of frames on one connection avoids that
    contention.

Servo update rate is left at S = 0.015 s for the internal control
loops, but note the servo PWM signal itself runs at 50 Hz (one pulse
every 20 ms) — polling faster than ~20 ms cannot make the physical
servo respond any sooner, so further lowering S has no effect on
perceived latency once it is already below that figure.

PIN NOTES SPECIFIC TO ESP32-C3:
  - GPIO0 / GPIO2: strapping pins, safe to drive once booted.
  - GPIO20 / GPIO21: default UART0 RX/TX — confirm your board flashes
    over native USB (e.g. ESP32-C3 SuperMini) before reusing them.
  - GPIO11-17: reserved for SPI flash on most C3 modules, avoided here.
  - GPIO9: strapping pin, tied to the BOOT button and must read HIGH at
    reset — never driven, so no servo signal line can hold the board in
    the bootloader. It is read as an input to toggle calibration mode.
  - GPIO4 / GPIO10: the last two genuinely free pins on a SuperMini,
    now taken by the LT / RT trigger servos.
  - LEDC (PWM) peripheral: 6 channels total on the C3 — all six are
    already committed to the six servos above.

LT / RT TRIGGER SERVOS (GPIO4, GPIO10) — DRIVEN BY RMT, NOT LEDC:
  These are the seventh and eighth servos, and the LEDC peripheral has
  no channel left for them: machine.PWM() would simply fail. Software
  bit-banging them is also not an option — asyncio on MicroPython
  resolves to about a millisecond, while a servo's entire 500-2500 us
  pulse range is only two milliseconds wide, so the arm would twitch
  across its full travel.

  The C3's RMT peripheral solves this exactly: it has 2 TX channels,
  both unused, and it clocks pulses out in hardware. With clock_div=80
  against the 80 MHz APB clock one RMT tick equals one microsecond, so
  a servo frame is written literally as (pulse_us, 20000 - pulse_us)
  and looped. Timing quality is on par with LEDC; the only cost is that
  exactly two such outputs exist, which is all that is needed here.

L298N VIBRATION MOTOR DRIVER (GPIO5-8, wired to IN1-IN4):
  GPIO5-8 are wired to the four IN pins, not the two EN pins — ENA and
  ENB are left on the driver board's default jumpers, which bridge
  each enable line permanently HIGH. No rewiring is required for that:
  it is the factory-default state of an L298N breakout as long as the
  ENA/ENB jumpers were never removed.

  With EN held permanently HIGH, the standard technique for speed
  control directly on the IN pins is to PWM one IN pin of a motor's
  pair while holding its partner LOW: the H-bridge chops the supply
  at the PWM duty cycle exactly as if EN itself were being PWMed.
  Holding the partner HIGH instead would run the same trick in the
  opposite fixed direction — irrelevant here since a vibration motor
  has no meaningful "reverse".
      GPIO5 -> IN1  (motor A intensity, PWM)
      GPIO6 -> IN2  (motor A, held LOW, fixed)
      GPIO7 -> IN3  (motor B intensity, PWM)
      GPIO8 -> IN4  (motor B, held LOW, fixed)

  IMPORTANT HARDWARE CONSTRAINT: the ESP32-C3's LEDC peripheral is
  already at its 6-channel ceiling (the six servos). The two PWMed IN
  pins therefore cannot use machine.PWM — there is no 7th or 8th
  hardware channel available. Motor intensity is instead driven by a
  software (bit-banged) PWM implemented below with asyncio. This is
  coarser and jitterier than LEDC, but adequate for a DC vibration
  motor's average voltage, which the motor's own mechanical inertia
  low-pass-filters.

FIRMWARE REQUIREMENT: machine.PWM.duty_ns() needs MicroPython >=1.20.
"""

import network
import asyncio
import random
import ujson
import ubinascii
import uhashlib
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
# RMT servo driver (same 50 Hz / microsecond-pulse contract as Servo
# above, but clocked out by the RMT peripheral instead of LEDC — see
# the docstring for why the trigger servos cannot use machine.PWM).
#
# clock_div=80 divides the 80 MHz APB clock down to 1 MHz, so one RMT
# tick is one microsecond and pulse widths are written directly in the
# units the servo datasheet uses.
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
        # Looping makes the peripheral repeat the two-entry sequence
        # forever without further CPU involvement — the servo sees a
        # continuous 50 Hz frame even while the control loop is idle.
        self._rmt.loop(True)
        self.move(home)

    def move(self, angle):
        angle = max(0, min(self._angle_range, angle))
        us = int(self._min_us + (self._max_us - self._min_us) * angle / self._angle_range)
        # Rewriting an unchanged waveform would restart the loop and
        # emit a truncated pulse, so only push real changes.
        if us == self._last_us:
            return
        self._last_us = us
        # (high_ticks, low_ticks), second argument being the initial
        # output level. It must be passed positionally: the esp32 RMT
        # methods are native and reject keyword arguments.
        self._rmt.write_pulses((us, self._period_us - us), 1)

    def deinit(self):
        self._rmt.loop(False)
        self._rmt.deinit()


# ---------------------------------------------------------------------
# Software PWM (for the two L298N enable pins — no hardware LEDC
# channels remain, since the six servos above already use all six).
# Bit-banged via asyncio at a modest frequency; sufficient for driving
# a DC vibration motor's average voltage, not for anything requiring
# clean, precise pulse timing.
# ---------------------------------------------------------------------
class SoftPWM:
    def __init__(self, pin, freq=200):
        self._pin = Pin(pin, Pin.OUT)
        self._pin.value(0)
        self._period = 1.0 / freq
        self.duty = 0.0   # 0.0 .. 1.0, updated externally, read by run()

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

# MicroPython hands one RMT channel to machine.bitstream (the neopixel
# timing generator) at startup, which on the C3 leaves only one of the
# two TX channels free — not enough for both triggers. Passing None
# releases it and falls machine.bitstream back to bit-banging; nothing
# in this project drives addressable LEDs, so that costs us nothing.
RMT.bitstream_channel(None)

# LT / RT triggers — RMT TX channels 0 and 1, the only two the C3 has.
# The two servos face each other across the controller, so they sweep in
# opposite directions: LT rests at 0 deg and counts up, RT rests at
# 180 deg and counts down. Both cover the same 60 deg of travel.
trigger_l = RMTServo(channel=0, pin=4, home=0)
trigger_r = RMTServo(channel=1, pin=10, home=180)

# L298N vibration motor driver — wired to IN1-IN4 (ENA/ENB left on
# their default jumpers, permanently HIGH). See docstring for the
# IN-pin PWM-chopping technique this relies on.
motorA_dir = Pin(6, Pin.OUT)   # IN2
motorA_dir.value(0)            # held low, fixed
motorA_en = SoftPWM(pin=5)     # IN1 — intensity (software PWM)

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

# BOOT button (GPIO9). Safe to read as an input — the strapping
# constraint is about what drives the pin at reset, and the button's
# own external pull-up already holds it HIGH there. Active LOW.
boot_button = Pin(9, Pin.IN, Pin.PULL_UP)

centerxl, centeryl = 90, 90
centerxr, centeryr = 90, 90
radius = 9      # max servo travel, in degrees, representing full joystick deflection
trigger_travel = 35  # degrees swept from released (0) to fully pulled (1.0)
trigger_l_rest = trigger_travel    # LT sweeps to 0
trigger_r_rest = 180 - trigger_travel  # RT is mirrored: 180 -> 120
S = 0.015       # internal control-loop interval, in seconds — see note above on the 20 ms PWM floor

buttons = {"A": 90, "B": 170, "Y": 40, "X": 140}
arrows = {"down": 40, "right": 140, "up": 90, "left": 180}

# ---------------------------------------------------------------------
# Calibration mode
#
# Pressing the BOOT button parks every servo at a known angle and holds
# it there, so the arms can be unscrewed and reseated against the real
# controller without the control loops fighting the adjustment. A second
# press hands control back to the browser.
#
# These are the angles the mechanism should be *assembled* at, which is
# not always the same as the angle a control loop treats as neutral —
# the triggers, for instance, are adjusted at their fully released
# position rather than mid-travel. Edit the values here to re-aim the
# rig; nothing else needs changing.
# ---------------------------------------------------------------------
CALIBRATION = {
    "colored": 90,               # coloured-button servo, off every button
    "arrow": 90,                 # d-pad servo, off every one of the four directions
    "joyL_x": centerxl,          # left stick, both axes centred
    "joyL_y": centeryl,
    "joyR_x": centerxr,          # right stick, both axes centred
    "joyR_y": centeryr,
    "trigL": trigger_l_rest+trigger_travel,     # triggers fully truigered
    "trigR": trigger_r_rest-trigger_travel,
}

# Name -> servo object, so CALIBRATION above stays a plain table of
# angles that can be edited without touching any wiring.
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
    "motorA": 0.0,        # vibration intensity, 0.0 .. 1.0
    "motorB": 0.0,
    "trigL": 0.0,         # analogue trigger travel, 0.0 (released) .. 1.0 (fully pulled)
    "trigR": 0.0,
    "calibrating": False, # True while the BOOT button has parked the servos
}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------
# Control coroutines
# ---------------------------------------------------------------------
async def button_task():
    # Edge-triggered, not continuous: the servo only issues a new move()
    # when state["button"] *changes* value (press -> exact target angle,
    # release -> a single +-10 deg nudge from wherever it already is).
    # Re-applying the same target every poll cycle would be harmless for
    # the "pressed" case but wrong for "released": jitter must happen
    # once per release, not accumulate every S seconds.
    prev = "__unset__"          # sentinel so the very first cycle also triggers
    current_angle = 90          # assumed neutral position before any press
    while True:
        if state["calibrating"]:
            # Resetting the sentinel means the current button state is
            # re-applied on the way out, rather than the servo silently
            # staying at its calibration angle until the next press.
            prev = "__unset__"
            # The servo is physically at its calibration angle now, so
            # the release-nudge below must be measured from there and
            # not from wherever it happened to be before the press.
            current_angle = CALIBRATION["colored"]
            await asyncio.sleep(S)
            continue
        b = state["button"]
        if b != prev:
            if b in buttons:
                current_angle = buttons[b]
            else:
                # released: move a small directio nto de-engage the button
                current_angle = current_angle + 25 if current_angle < 90 else current_angle -25
            colored.move(current_angle)
            prev = b
        await asyncio.sleep(S)


async def arrow_task():
    prev = "__unset__"          # sentinel so the very first cycle also triggers
    current_angle = 65          # assumed neutral position before any press
    while True:
        if state["calibrating"]:
            prev = "__unset__"          # see button_task
            current_angle = CALIBRATION["arrow"]
            await asyncio.sleep(S)
            continue
        a = state["arrow"]
        if a != prev:
            if a in arrows:
                current_angle = arrows[a]
            else:
                # released: move a small direction to de-engage the arrow.
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
    # Both triggers share one coroutine: they are analogue like the
    # joysticks (continuously re-applied, not edge-triggered), but
    # RMTServo.move() already suppresses redundant writes, so polling
    # them together costs nothing when nothing is moving.
    while True:
        if state["calibrating"]:
            await asyncio.sleep(S)
            continue
        trigger_l.move(trigger_l_rest + state["trigL"] * trigger_travel)
        trigger_r.move(trigger_r_rest - state["trigR"] * trigger_travel)
        await asyncio.sleep(S)


async def motor_a_task():
    # Silenced during calibration: a buzzing rig is no help when you are
    # trying to seat a servo horn by hand.
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
    # Toggle calibration mode on each press of the BOOT button.
    #
    # While calibrating this keeps re-applying the table every cycle
    # rather than issuing one move on entry. That costs nothing (the
    # servo drivers are just rewriting an identical pulse width) and it
    # removes a race: the control loops check the flag on their own S
    # interval, so one of them can still push a stale angle out just
    # after the mode changes. Continuously re-asserting wins that race.
    prev = 1                       # active low, released == 1
    while True:
        v = boot_button.value()
        if prev == 1 and v == 0:                  # falling edge = press
            state["calibrating"] = not state["calibrating"]
            status_led(state["calibrating"])
            print("calibration mode:", "ON" if state["calibrating"] else "OFF")
            await asyncio.sleep(0.2)              # ride out contact bounce
            v = boot_button.value()
        prev = v

        if state["calibrating"]:
            for name, angle in CALIBRATION.items():
                SERVOS[name].move(angle)

        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------
# Wi-Fi access point
# ---------------------------------------------------------------------
def start_ap(ssid="ghost-player", password="cognitivsculptur"):
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=ssid, password=password, authmode=network.AUTH_WPA2_PSK)
    while not ap.active():
        pass
    return ap


# ---------------------------------------------------------------------
# Minimal WebSocket server (RFC 6455 subset: text frames only, no ping/
# pong, no fragmentation handling — sufficient for short JSON control
# messages on a trusted local AP).
# ---------------------------------------------------------------------
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept_key(client_key):
    digest = uhashlib.sha1((client_key + WS_MAGIC).encode()).digest()
    return ubinascii.b2a_base64(digest).decode().strip()


async def ws_read_frame(reader):
    b1 = await reader.readexactly(1)
    b2 = await reader.readexactly(1)
    opcode = b1[0] & 0x0F
    masked = b2[0] & 0x80
    length = b2[0] & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    mask = await reader.readexactly(4) if masked else None
    data = await reader.readexactly(length) if length else b""
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data


def ws_encode_text(msg):
    payload = msg.encode()
    n = len(payload)
    if n < 126:
        header = bytes([0x81, n])
    elif n < 65536:
        header = bytes([0x81, 126]) + n.to_bytes(2, "big")
    else:
        header = bytes([0x81, 127]) + n.to_bytes(8, "big")
    return header + payload


async def handle_ws(reader, writer):
    try:
        while True:
            opcode, data = await ws_read_frame(reader)
            if opcode == 0x8:          # close frame
                break
            if opcode == 0x1:          # text frame
                try:
                    payload = ujson.loads(data)
                except ValueError:
                    continue
                for k in ("button", "arrow"):
                    if k in payload:
                        state[k] = payload[k]
                for k in ("joyL", "joyR"):
                    if k in payload:
                        x, y = payload[k]
                        state[k] = (clamp(float(x), -1, 1), clamp(float(y), -1, 1))
                for k in ("motorA", "motorB", "trigL", "trigR"):
                    if k in payload:
                        state[k] = clamp(float(payload[k]), 0.0, 1.0)
    except Exception as e:
        print("ws closed:", e)
    finally:
        await writer.wait_closed()


# ---------------------------------------------------------------------
# HTML / JS controller UI
# ---------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ghost-player</title>
<style>
  body{background:#0a0a0a;color:#e8ff2e;font-family:'Space Mono',monospace;
       display:flex;flex-direction:column;align-items:center;gap:20px;padding:20px}
  .pad{width:140px;height:140px;border:2px solid #e8ff2e;border-radius:50%;
       position:relative;touch-action:none}
  .stick{width:36px;height:36px;background:#e8ff2e;border-radius:50%;
         position:absolute;left:52px;top:52px}
  .row{display:flex;gap:14px}
  button{background:none;border:2px solid #e8ff2e;color:#e8ff2e;
         width:56px;height:56px;border-radius:50%;font-family:inherit}
  button:active{background:#e8ff2e;color:#0a0a0a}
  .arrows button{border-radius:6px;width:64px}
  #status{font-size:11px;opacity:0.6}
  .faders{display:flex;gap:24px;align-items:flex-end}
  .fader{display:flex;flex-direction:column;align-items:center;gap:6px;font-size:11px}
  .fader input[type=range]{writing-mode:vertical-lr;direction:rtl;width:24px;height:120px;accent-color:#e8ff2e}
  .sticks{display:flex;gap:14px;align-items:center}
  .trigger input[type=range]{height:150px}
</style></head>
<body>
<div id="status">connecting...</div>
<div class="sticks">
  <div class="fader trigger">LT<input type="range" id="trigL" min="0" max="100" value="0"></div>
  <div class="pad" id="padL"><div class="stick" id="stickL"></div></div>
  <div class="pad" id="padR"><div class="stick" id="stickR"></div></div>
  <div class="fader trigger">RT<input type="range" id="trigR" min="0" max="100" value="0"></div>
</div>
<div class="faders">
  <div class="fader">MOTOR A<input type="range" id="faderA" min="0" max="100" value="0"></div>
  <div class="fader">MOTOR B<input type="range" id="faderB" min="0" max="100" value="0"></div>
</div>
<div class="row">
  <button id="btnX">X</button>
  <button id="btnY">Y</button>
  <button id="btnA">A</button>
  <button id="btnB">B</button>
</div>
<div class="row arrows">
  <button id="btnLeft">&#8592;</button>
  <button id="btnUp">&#8593;</button>
  <button id="btnDown">&#8595;</button>
  <button id="btnRight">&#8594;</button>
</div>
<script>
let pending = {};
let ws;

function connect(){
  ws = new WebSocket("ws://" + location.host + "/ws");
  ws.onopen = () => document.getElementById("status").textContent = "connected";
  ws.onclose = () => {
    document.getElementById("status").textContent = "reconnecting...";
    setTimeout(connect, 500);
  };
}
connect();

// Coalesce rapid inputs (touchmove/mousemove can fire far faster than
// the servo loop can act on) and flush at a fixed rate over the single
// open socket, instead of one message per raw DOM event. Used only for
// the joysticks and faders, which genuinely produce a high-frequency
// stream.
function queue(partial){ Object.assign(pending, partial); }
setInterval(() => {
  if (ws.readyState === 1 && Object.keys(pending).length){
    ws.send(JSON.stringify(pending));
    pending = {};
  }
}, 30); // ~33 Hz flush rate, comfortably above the 50 Hz servo refresh floor

// Discrete, low-frequency events (button/arrow press and release) are
// sent immediately rather than through the queue above: batching them
// risks a fast press-then-release landing in the same 30 ms window,
// which would collapse to "released" and silently drop the press.
function sendNow(payload){
  if (ws.readyState === 1) ws.send(JSON.stringify(payload));
}

// Shared press/hold binding: sends {key: value} while held, {key: null}
// on release. Used identically for the coloured buttons (key="button")
// and the arrow buttons (key="arrow") so both drive their respective
// servo through the same press -> target / release -> de-engage logic
// on the ESP32-C3 side.
function bindHold(id, key, value){
  const el = document.getElementById(id);
  const press = () => sendNow({[key]: value});
  const release = () => sendNow({[key]: null});
  el.addEventListener("mousedown", press);
  el.addEventListener("mouseup", release);
  el.addEventListener("mouseleave", release); // covers pointer dragged off the button while held
  el.addEventListener("touchstart", e => { e.preventDefault(); press(); });
  el.addEventListener("touchend", e => { e.preventDefault(); release(); });
}
bindHold("btnX", "button", "X");
bindHold("btnY", "button", "Y");
bindHold("btnA", "button", "A");
bindHold("btnB", "button", "B");
bindHold("btnLeft", "arrow", "left");
bindHold("btnUp", "arrow", "up");
bindHold("btnDown", "arrow", "down");
bindHold("btnRight", "arrow", "right");

function bindPad(padId, stickId, key){
  const pad = document.getElementById(padId);
  const stick = document.getElementById(stickId);
  const R = 70, SR = 18;
  function onMove(e){
    const rect = pad.getBoundingClientRect();
    const t = e.touches ? e.touches[0] : e;
    let dx = (t.clientX - rect.left - R) / R;
    let dy = (t.clientY - rect.top - R) / R;
    const mag = Math.min(1, Math.hypot(dx, dy));
    const ang = Math.atan2(dy, dx);
    dx = Math.cos(ang) * mag; dy = Math.sin(ang) * mag;
    stick.style.left = (R + dx*(R-SR) - SR) + "px";
    stick.style.top  = (R + dy*(R-SR) - SR) + "px";
    const payload = {}; payload[key] = [dx, dy];
    queue(payload);
  }
  function onEnd(){
    stick.style.left = "52px"; stick.style.top = "52px";
    const payload = {}; payload[key] = [0,0];
    queue(payload);
  }
  pad.addEventListener("touchmove", onMove);
  pad.addEventListener("touchstart", onMove);
  pad.addEventListener("touchend", onEnd);
  pad.addEventListener("mousedown", e => {
    const mv = ev => onMove(ev);
    document.addEventListener("mousemove", mv);
    document.addEventListener("mouseup", () => {
      document.removeEventListener("mousemove", mv);
      onEnd();
    }, {once:true});
  });
}
bindPad("padL","stickL","joyL");
bindPad("padR","stickR","joyR");

function bindFader(id, key){
  const el = document.getElementById(id);
  el.addEventListener("input", () => {
    const payload = {}; payload[key] = el.value / 100;
    queue(payload);
  });
}
bindFader("faderA", "motorA");
bindFader("faderB", "motorB");

// The triggers use the same slider widget as the motor faders, but
// spring back to zero when let go, the way a real analogue trigger
// does — a motor fader is a setting you leave somewhere, a trigger is
// something you hold. Without this a trigger servo would stay pressed
// against the controller indefinitely after the pointer left the page.
function bindTrigger(id, key){
  const el = document.getElementById(id);
  const send = () => { const p = {}; p[key] = el.value / 100; queue(p); };
  const release = () => { el.value = 0; send(); };
  el.addEventListener("input", send);
  el.addEventListener("mouseup", release);
  el.addEventListener("mouseleave", release);
  el.addEventListener("touchend", release);
  el.addEventListener("touchcancel", release);
}
bindTrigger("trigL", "trigL");
bindTrigger("trigR", "trigR");
</script>
</body></html>"""


async def handle_client(reader, writer):
    try:
        request_line = await reader.readline()
        method, path, _ = request_line.decode().split(" ", 2)
        headers = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
            k, v = line.decode().split(":", 1)
            headers[k.strip().lower()] = v.strip()

        if method == "GET" and path == "/ws" and headers.get("upgrade", "").lower() == "websocket":
            accept = ws_accept_key(headers["sec-websocket-key"])
            writer.write(
                ("HTTP/1.1 101 Switching Protocols\r\n"
                 "Upgrade: websocket\r\n"
                 "Connection: Upgrade\r\n"
                 "Sec-WebSocket-Accept: {}\r\n\r\n").format(accept).encode()
            )
            await writer.drain()
            await handle_ws(reader, writer)
            return

        elif method == "GET" and path == "/":
            body = PAGE.encode()
            writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n")
            writer.write("Content-Length: {}\r\n\r\n".format(len(body)).encode())
            writer.write(body)

        else:
            writer.write(b"HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\n\r\n")

        await writer.drain()
    except Exception as e:
        print("request error:", e)
    finally:
        await writer.wait_closed()


async def main():
    ap = start_ap()
    print("AP active:", ap.ifconfig())

    server = await asyncio.start_server(handle_client, "0.0.0.0", 80)

    await asyncio.gather(
        server.wait_closed(),
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
