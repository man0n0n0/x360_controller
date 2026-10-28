"""RMT debug for the LT / RT trigger servos — ESP32-C3 / MicroPython
-----------------------------------------------------------------
Runs on the board, not on the host:

    mpremote run tools/rmt_debug.py

Hard-reset the board first with BOOT held (so boot.py skips main and no
RMT channel is already taken), then run it. Every step prints what it is
doing and what the servo should be doing, so watch the arms while it
runs. Nothing here touches the six LEDC servos or the motors.

Steps:
  1. firmware / chip info and the RMT API this build actually exposes
  2. releasing the bitstream channel
  3. opening channels 0 and 1 with every constructor form we know of
  4. reading back the real tick clock (catches the 80 vs 40 MHz case)
  5. holding a fixed 1500 us pulse, looped — the "stiff at centre" test
  6. optional loopback measurement, if a jumper is fitted (see LOOPBACK)
  7. a slow sweep across the trigger travel used by main_offline.py
"""

import sys
import time
import machine
from machine import Pin
import esp32
from esp32 import RMT

LT_PIN = 4
RT_PIN = 10
HOLD_S = 3          # seconds each visible test is held

# Optional: jumper LT_PIN (GPIO4) to a free input pin and set its number
# here to measure the real pulse width and period with time_pulse_us.
# GPIO5 works if the L298N is unplugged. Leave None to skip.
LOOPBACK = None


def title(t):
    print()
    print("=" * 60)
    print(t)
    print("=" * 60)


def err(e):
    # OSError from IDF carries (code, 'ESP_ERR_...') — print both.
    return "%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------------
title("1. Firmware and RMT API")
print("implementation:", sys.implementation)
print("platform      :", sys.platform)
try:
    print("machine       :", machine.unique_id().hex(), "@", machine.freq() // 1000000, "MHz CPU")
except Exception as e:
    print("machine.freq  :", err(e))
print("RMT attributes:", [a for a in dir(RMT) if not a.startswith("_")])
for name in ("bitstream_channel", "source_freq", "loop", "loop_count",
             "write_pulses", "wait_done", "clock_div", "active"):
    print("  has %-18s %s" % (name + "?", hasattr(RMT, name)))


# ---------------------------------------------------------------------
title("2. Release the bitstream channel")
if hasattr(RMT, "bitstream_channel"):
    try:
        print("before:", RMT.bitstream_channel())
    except Exception as e:
        print("query failed:", err(e))
    try:
        RMT.bitstream_channel(None)
        print("released OK, now:", RMT.bitstream_channel())
    except Exception as e:
        print("release FAILED:", err(e))
else:
    print("RMT.bitstream_channel not available on this build")


# ---------------------------------------------------------------------
title("3. Open channels 0 and 1")

CTOR_FORMS = (
    ("clock_div=80 (what main_offline uses)", {"clock_div": 80}),
    ("resolution_hz=1_000_000 (newer API)", {"resolution_hz": 1000000}),
    ("defaults", {}),
)


def open_rmt(ch, pin):
    for label, kw in CTOR_FORMS:
        try:
            r = RMT(ch, pin=Pin(pin), **kw)
            print("ch%d pin%d  %-40s OK  -> %r" % (ch, pin, label, r))
            return r, label, kw
        except Exception as e:
            print("ch%d pin%d  %-40s %s" % (ch, pin, label, err(e)))
    return None, None, None


lt, lt_form, lt_kw = open_rmt(0, LT_PIN)
rt, rt_form, rt_kw = open_rmt(1, RT_PIN)

if lt is None and rt is None:
    print()
    print("No channel could be opened. If every error is ESP_ERR_NOT_FOUND,")
    print("the channels are still held: hard reset (RST / replug) with BOOT")
    print("held and run this again before anything else.")
    sys.exit()


# ---------------------------------------------------------------------
title("4. Tick clock")
# Work out how many ticks make one microsecond on this channel. If it is
# not 1, the pulses main_offline.py writes are the wrong length.
ticks_per_us = None
if hasattr(RMT, "source_freq"):
    try:
        src = RMT.source_freq()
        print("source_freq():", src, "Hz")
        if lt_kw and "clock_div" in lt_kw:
            ticks_per_us = src / lt_kw["clock_div"] / 1000000
    except Exception as e:
        print("source_freq() failed:", err(e))
if lt is not None and hasattr(lt, "clock_div"):
    try:
        print("lt.clock_div():", lt.clock_div())
    except Exception as e:
        print("lt.clock_div() failed:", err(e))
if lt_kw and "resolution_hz" in lt_kw:
    ticks_per_us = lt_kw["resolution_hz"] / 1000000
if ticks_per_us is None:
    print("could not determine tick length — assuming 1 tick = 1 us")
    ticks_per_us = 1.0
else:
    print("=> 1 tick = %.3f us  (%s)" % (1 / ticks_per_us,
          "OK" if abs(ticks_per_us - 1) < 0.01 else
          "WRONG: main_offline pulses are %.1fx too long" % (1 / ticks_per_us)))


def us(t):
    return max(1, int(t * ticks_per_us))


# ---------------------------------------------------------------------
def start_loop(r, pulse_us, period_us=20000):
    """Start a repeating servo frame; return the method that worked."""
    frame = (us(pulse_us), us(period_us - pulse_us))
    # Old API: loop(True) then write_pulses(durations, start_level).
    if hasattr(r, "loop"):
        try:
            r.loop(True)
            r.write_pulses(frame, 1)
            return "loop(True) + write_pulses(frame, 1)"
        except Exception as e:
            print("   loop/write_pulses failed:", err(e))
    # Newer API: loop_count(-1) means repeat forever.
    if hasattr(r, "loop_count"):
        try:
            r.loop_count(-1)
            r.write_pulses(frame, 1)
            return "loop_count(-1) + write_pulses(frame, 1)"
        except Exception as e:
            print("   loop_count/write_pulses failed:", err(e))
    # Last resort: alternating level list.
    try:
        r.write_pulses(frame, [1, 0])
        return "write_pulses(frame, [1, 0]) — single shot, NOT looping"
    except Exception as e:
        print("   write_pulses(frame, [1,0]) failed:", err(e))
    return None


def stop_loop(r):
    for fn, args in (("loop", (False,)), ("loop_count", (0,))):
        if hasattr(r, fn):
            try:
                getattr(r, fn)(*args)
            except Exception:
                pass


title("5. Hold 1500 us (centre) for %d s on each channel" % HOLD_S)
print("EXPECT: the arm goes to centre and stays STIFF (push it gently).")
print("  twitches then goes limp -> looping is broken on this firmware")
print("  no move / slams to an end -> tick clock or wiring")
for name, r in (("LT", lt), ("RT", rt)):
    if r is None:
        print(name, ": channel not open, skipped")
        continue
    how = start_loop(r, 1500)
    print("%s: %s" % (name, how))
    time.sleep(HOLD_S)
    stop_loop(r)


# ---------------------------------------------------------------------
if LOOPBACK is not None and lt is not None:
    title("6. Loopback measurement LT GPIO%d -> GPIO%d" % (LT_PIN, LOOPBACK))
    probe = Pin(LOOPBACK, Pin.IN)
    start_loop(lt, 1500)
    time.sleep_ms(100)
    highs = [machine.time_pulse_us(probe, 1, 100000) for _ in range(5)]
    lows = [machine.time_pulse_us(probe, 0, 100000) for _ in range(5)]
    stop_loop(lt)
    print("high us:", highs, "(expect ~1500)")
    print("low  us:", lows, "(expect ~18500)")
    print("negative values = timeout: no signal reaching the probe pin")


# ---------------------------------------------------------------------
title("7. Sweep the trigger travel used by main_offline.py")
# Same mapping as RMTServo: 0..180 deg -> 500..2500 us.
def angle_us(a):
    return int(500 + 2000 * a / 180)


SWEEPS = (("LT", lt, 0, 35), ("RT", rt, 180, 145))
for name, r, rest, pulled in SWEEPS:
    if r is None:
        continue
    print("%s: %d deg -> %d deg -> %d deg (watch it pull and release)" % (name, rest, pulled, rest))
    step = 1 if pulled > rest else -1
    for a in list(range(rest, pulled + step, step * 5)) + list(range(pulled, rest - step, -step * 5)):
        stop_loop(r)
        start_loop(r, angle_us(a))
        time.sleep_ms(150)
    time.sleep(1)
    stop_loop(r)


# ---------------------------------------------------------------------
title("Done")
for r in (lt, rt):
    if r is not None:
        try:
            r.deinit()
        except Exception as e:
            print("deinit:", err(e))
print("Copy everything above and send it back.")
