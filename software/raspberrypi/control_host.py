#!/usr/bin/env python3
"""Ghost-player master — Linux host for main_offline.py

Drives the ESP32-C3 controller rig over USB serial: a live Tk interface
with the same layout as the old browser UI, plus a recorder that
captures a session to disk and replays it with its original timing.

    python3 control_host.py                 # pick the port in the UI
    python3 control_host.py /dev/ttyACM0    # or name it up front

Requires pyserial (`pip install pyserial`); Tk comes with CPython.

DESIGN NOTES

Baud rate is meaningless here and is not configurable. The C3's port is
native USB CDC, not a UART behind a bridge chip — the line settings are
negotiated away and frames move at USB speed regardless of what number
is passed to pySerial.

Outgoing input is coalesced at FLUSH_MS rather than sent per event, for
the reason the WebSocket build did the same: a mouse drag fires far
faster than a servo can act, and the useful ceiling is the 50 Hz servo
refresh. One exception — button and arrow presses bypass the queue and
go out immediately, because a fast press-and-release landing inside a
single flush window would collapse to "released" and drop the press.

The recorder stores *deltas*, not full frames: each entry is the set of
keys that actually changed, with the timestamp it changed at. A session
where only the left stick moves records only left-stick keys, so a
sequence stays readable and hand-editable as JSON.

Playback runs on its own thread and writes to the port directly instead
of going through the queue, so replay timing is the recorded timing and
not the flush grid.
"""

import json
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is required:  pip install pyserial")


FLUSH_MS = 30        # outgoing coalescing interval (~33 Hz)
HEARTBEAT_MS = 400   # must stay well under the device's LINK_TIMEOUT_MS
PAD_SIZE = 160       # joystick pad diameter, pixels
KNOB = 20            # stick knob radius, pixels

BG = "#4a4a4a"
FG = "#4ee660"
DIM = "#4ee660"

# Every controllable channel, in the order a sequence file lists them.
CHANNELS = ("joyL", "joyR", "trigL", "trigR", "motorA", "motorB",
            "button", "arrow")

NEUTRAL = {
    "joyL": [0.0, 0.0], "joyR": [0.0, 0.0],
    "trigL": 0.0, "trigR": 0.0,
    "motorA": 0.0, "motorB": 0.0,
    "button": None, "arrow": None,
}


# ---------------------------------------------------------------------
# Serial link
# ---------------------------------------------------------------------
class Link:
    """Line-delimited JSON over the CDC port, with a reader thread."""

    def __init__(self, on_event, on_status):
        self._ser = None
        self._lock = threading.Lock()
        self._rx = None
        self._stop = threading.Event()
        self.on_event = on_event      # called from the reader thread
        self.on_status = on_status

    @property
    def connected(self):
        return self._ser is not None and self._ser.is_open

    def open(self, port):
        self.close()
        # A short read timeout keeps the reader thread responsive to
        # _stop even when the device is silent.
        self._ser = serial.Serial(port, 115200, timeout=0.1)
        self._stop.clear()
        self._rx = threading.Thread(target=self._reader, daemon=True)
        self._rx.start()
        self.on_status("connected: %s" % port)
        self.send({"get": 1})

    def close(self):
        self._stop.set()
        if self._rx and self._rx.is_alive():
            self._rx.join(timeout=0.5)
        self._rx = None
        if self._ser:
            try:
                # Leave the rig safe rather than frozen mid-input.
                self._raw_write({"stop": 1})
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def _raw_write(self, obj):
        self._ser.write((json.dumps(obj) + "\n").encode())

    def send(self, obj):
        if not self.connected:
            return
        try:
            with self._lock:
                self._raw_write(obj)
        except Exception as e:
            self.on_status("write failed: %s" % e)
            self._ser = None

    def _reader(self):
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256)
            except Exception:
                break
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    self.on_event(json.loads(line.decode(errors="replace")))
                except ValueError:
                    # The device also carries MicroPython tracebacks and
                    # REPL noise on this port; surface it rather than
                    # silently dropping something that may be an error.
                    self.on_event({"ev": "raw", "msg": line.decode(errors="replace")})


# ---------------------------------------------------------------------
# Recorder / player
# ---------------------------------------------------------------------
class Sequence:
    def __init__(self):
        self.frames = []          # [[t_seconds, {delta}], ...]
        self._t0 = None

    @property
    def duration(self):
        return self.frames[-1][0] if self.frames else 0.0

    def start(self):
        self.frames = []
        self._t0 = time.monotonic()

    def capture(self, delta):
        if self._t0 is None or not delta:
            return
        self.frames.append([round(time.monotonic() - self._t0, 4), dict(delta)])

    def stop(self):
        self._t0 = None

    def save(self, path):
        with open(path, "w") as f:
            json.dump({"version": 1, "channels": list(CHANNELS),
                       "duration": self.duration, "frames": self.frames},
                      f, indent=1)

    def load(self, path):
        with open(path) as f:
            data = json.load(f)
        self.frames = [[float(t), d] for t, d in data["frames"]]


# ---------------------------------------------------------------------
# Joystick pad widget
# ---------------------------------------------------------------------
class JoyPad(tk.Canvas):
    def __init__(self, master, label, on_change):
        super().__init__(master, width=PAD_SIZE, height=PAD_SIZE,
                         bg=BG, highlightthickness=0)
        self.on_change = on_change
        r = PAD_SIZE / 2
        self.create_oval(2, 2, PAD_SIZE - 2, PAD_SIZE - 2, outline=FG, width=2)
        self.create_text(r, 12, text=label, fill=DIM, font=("TkFixedFont", 8))
        self.knob = self.create_oval(r - KNOB, r - KNOB, r + KNOB, r + KNOB,
                                     fill=FG, outline="")
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonPress-1>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)

    def _drag(self, e):
        r = PAD_SIZE / 2
        dx = (e.x - r) / r
        dy = (e.y - r) / r
        # Clamp to the unit circle, not the unit square: dragging into a
        # corner must not report a magnitude of 1.41 on the diagonal.
        mag = (dx * dx + dy * dy) ** 0.5
        if mag > 1.0:
            dx, dy = dx / mag, dy / mag
        self.set(dx, dy)
        self.on_change([round(dx, 3), round(dy, 3)])

    def _release(self, _e):
        self.set(0.0, 0.0)
        self.on_change([0.0, 0.0])

    def set(self, dx, dy):
        """Move the knob without firing the callback (used by playback)."""
        r = PAD_SIZE / 2
        travel = r - KNOB - 2
        cx, cy = r + dx * travel, r + dy * travel
        self.coords(self.knob, cx - KNOB, cy - KNOB, cx + KNOB, cy + KNOB)


# ---------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self, port=None):
        super().__init__()
        self.title("ghost-player master")
        self.configure(bg=BG)

        self.link = Link(self._on_device_event, self._set_status)
        self.seq = Sequence()
        self.pending = {}
        self.recording = False
        self.playing = False
        self._play_stop = threading.Event()
        self._suppress = False        # guards widget->send feedback loops

        self._build_ui()
        self.after(FLUSH_MS, self._flush)
        self.after(HEARTBEAT_MS, self._heartbeat)
        self.protocol("WM_DELETE_WINDOW", self._quit)

        if port:
            self.port_var.set(port)
            self._connect()

    # -- transport ----------------------------------------------------
    def queue(self, **kw):
        """Coalesce continuous input until the next flush."""
        if self._suppress:
            return
        self.pending.update(kw)

    def send_now(self, **kw):
        """Discrete input: bypass the queue so a fast press is not lost."""
        if self._suppress:
            return
        if self.recording:
            self.seq.capture(kw)
        self.link.send(kw)

    def _flush(self):
        if self.pending:
            if self.recording:
                self.seq.capture(self.pending)
            self.link.send(self.pending)
            self.pending = {}
        self.after(FLUSH_MS, self._flush)

    def _heartbeat(self):
        # Keeps the device's failsafe from firing during idle periods,
        # which are otherwise indistinguishable from a dead master.
        if self.link.connected and not self.pending:
            self.link.send({"hb": 1})
        self.after(HEARTBEAT_MS, self._heartbeat)

    # -- UI -----------------------------------------------------------
    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TButton", background=BG, foreground=FG)
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("TScale", background=BG)

        pad = dict(padx=6, pady=4)

        # --- connection row
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", **pad)
        self.port_var = tk.StringVar()
        ports = [p.device for p in list_ports.comports()] or ["/dev/ttyACM0"]
        self.port_var.set(ports[0])
        ttk.Combobox(top, textvariable=self.port_var, values=ports,
                     width=18).pack(side="left")
        ttk.Button(top, text="connect", command=self._connect).pack(side="left", padx=4)
        ttk.Button(top, text="disconnect", command=self._disconnect).pack(side="left")
        ttk.Button(top, text="STOP", command=self._panic).pack(side="left", padx=12)
        self.status = tk.Label(top, text="idle", bg=BG, fg=DIM,
                               font=("TkFixedFont", 9))
        self.status.pack(side="left", padx=8)

        # --- sticks and triggers
        mid = tk.Frame(self, bg=BG)
        mid.pack(**pad)
        self.trigL = self._fader(mid, "LT", "trigL", spring=True)
        self.padL = JoyPad(mid, "L", lambda v: self.queue(joyL=v))
        self.padL.pack(side="left", padx=6)
        self.padR = JoyPad(mid, "R", lambda v: self.queue(joyR=v))
        self.padR.pack(side="left", padx=6)
        self.trigR = self._fader(mid, "RT", "trigR", spring=True)

        # --- vibration motors
        mot = tk.Frame(self, bg=BG)
        mot.pack(**pad)
        self.motorA = self._fader(mot, "MOTOR A", "motorA")
        self.motorB = self._fader(mot, "MOTOR B", "motorB")

        # --- face buttons and d-pad
        btns = tk.Frame(self, bg=BG)
        btns.pack(**pad)
        for name in ("X", "Y", "A", "B"):
            self._hold_button(btns, name, "button", name)
        arr = tk.Frame(self, bg=BG)
        arr.pack(**pad)
        for label, val in (("←", "left"), ("↑", "up"), ("↓", "down"), ("→", "right")):
            self._hold_button(arr, label, "arrow", val)

        # --- transport
        trans = tk.Frame(self, bg=BG)
        trans.pack(fill="x", **pad)
        self.rec_btn = ttk.Button(trans, text="● rec", command=self._toggle_record)
        self.rec_btn.pack(side="left")
        self.play_btn = ttk.Button(trans, text="▶ play", command=self._toggle_play)
        self.play_btn.pack(side="left", padx=4)
        self.loop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(trans, text="loop", variable=self.loop_var).pack(side="left", padx=4)
        ttk.Button(trans, text="save", command=self._save).pack(side="left", padx=4)
        ttk.Button(trans, text="load", command=self._load).pack(side="left")
        self.seq_label = tk.Label(trans, text="no sequence", bg=BG, fg=DIM,
                                  font=("TkFixedFont", 9))
        self.seq_label.pack(side="left", padx=10)

        # --- device log
        self.log = tk.Text(self, height=6, width=64, bg="#111", fg=DIM,
                           font=("TkFixedFont", 9), highlightthickness=0, bd=0)
        self.log.pack(fill="both", expand=True, **pad)

    def _fader(self, parent, label, key, spring=False):
        """Vertical 0..1 slider. Spring faders snap back on release, the
        way a real analogue trigger does; motor faders stay put."""
        box = tk.Frame(parent, bg=BG)
        box.pack(side="left", padx=8)
        tk.Label(box, text=label, bg=BG, fg=FG, font=("TkFixedFont", 8)).pack()
        var = tk.DoubleVar(value=0.0)
        sc = tk.Scale(box, from_=1.0, to=0.0, resolution=0.01, orient="vertical",
                      length=130, variable=var, showvalue=0, bg=BG, fg=FG,
                      troughcolor="#1c1c1c", highlightthickness=0, bd=0,
                      activebackground=FG,
                      command=lambda v, k=key: self.queue(**{k: float(v)}))
        sc.pack()
        if spring:
            sc.bind("<ButtonRelease-1>", lambda e, v=var: v.set(0.0))
        return var

    def _hold_button(self, parent, label, key, value):
        b = tk.Button(parent, text=label, width=4, bg=BG, fg=FG,
                      activebackground=FG, activeforeground=BG,
                      highlightbackground=FG, font=("TkFixedFont", 11))
        b.pack(side="left", padx=4)
        b.bind("<ButtonPress-1>", lambda e: self.send_now(**{key: value}))
        b.bind("<ButtonRelease-1>", lambda e: self.send_now(**{key: None}))
        return b

    # -- actions ------------------------------------------------------
    def _connect(self):
        try:
            self.link.open(self.port_var.get())
        except Exception as e:
            messagebox.showerror("connect", str(e))
            self._set_status("not connected")

    def _disconnect(self):
        self.link.close()
        self._set_status("disconnected")

    def _panic(self):
        self._play_stop.set()
        self.pending = {}
        self.link.send({"stop": 1})
        self._reflect(NEUTRAL)
        self._set_status("STOP sent")

    def _toggle_record(self):
        if self.playing:
            return
        self.recording = not self.recording
        if self.recording:
            self.seq.start()
            self.rec_btn.config(text="■ stop")
            self._set_status("recording")
        else:
            self.seq.stop()
            self.rec_btn.config(text="● rec")
            self._set_status("recorded %d frames" % len(self.seq.frames))
            self._update_seq_label()

    def _toggle_play(self):
        if self.playing:
            self._play_stop.set()
            return
        if not self.seq.frames:
            messagebox.showinfo("play", "nothing recorded or loaded")
            return
        if self.recording:
            self._toggle_record()
        self._play_stop.clear()
        self.playing = True
        self.play_btn.config(text="■ stop")
        threading.Thread(target=self._play_worker, daemon=True).start()

    def _play_worker(self):
        try:
            while True:
                t0 = time.monotonic()
                for t, delta in self.seq.frames:
                    # Sleep against absolute elapsed time, not cumulative
                    # gaps: the latter drifts, and a long sequence would
                    # end up noticeably behind its recorded timing.
                    wait = t - (time.monotonic() - t0)
                    if wait > 0 and self._play_stop.wait(wait):
                        return
                    if self._play_stop.is_set():
                        return
                    self.link.send(delta)
                    self.after(0, self._reflect, delta)
                if not self.loop_var.get():
                    return
        finally:
            self.link.send({"stop": 1})
            self.after(0, self._play_done)

    def _play_done(self):
        self.playing = False
        self.play_btn.config(text="▶ play")
        self._reflect(NEUTRAL)
        self._set_status("playback finished")

    def _reflect(self, delta):
        """Mirror device state onto the widgets during playback, without
        the widget callbacks queueing it straight back to the device."""
        self._suppress = True
        try:
            if "joyL" in delta:
                self.padL.set(*delta["joyL"])
            if "joyR" in delta:
                self.padR.set(*delta["joyR"])
            for key, var in (("trigL", self.trigL), ("trigR", self.trigR),
                             ("motorA", self.motorA), ("motorB", self.motorB)):
                if key in delta:
                    var.set(delta[key])
        finally:
            self._suppress = False

    def _save(self):
        if not self.seq.frames:
            messagebox.showinfo("save", "nothing recorded")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("sequence", "*.json")])
        if path:
            self.seq.save(path)
            self._set_status("saved %s" % path)

    def _load(self):
        path = filedialog.askopenfilename(filetypes=[("sequence", "*.json")])
        if not path:
            return
        try:
            self.seq.load(path)
        except Exception as e:
            messagebox.showerror("load", str(e))
            return
        self._update_seq_label()
        self._set_status("loaded %s" % path)

    def _update_seq_label(self):
        self.seq_label.config(text="%d frames / %.1fs"
                              % (len(self.seq.frames), self.seq.duration))

    # -- device feedback ----------------------------------------------
    def _on_device_event(self, msg):
        # Called on the reader thread — hop to the Tk thread before
        # touching any widget.
        self.after(0, self._show_event, msg)

    def _show_event(self, msg):
        ev = msg.get("ev")
        if ev == "calib":
            self._set_status("CALIBRATION %s" % ("ON" if msg.get("on") else "OFF"))
        elif ev == "failsafe":
            self._set_status("device failsafe: link lost")
        self.log.insert("end", json.dumps(msg) + "\n")
        self.log.see("end")

    def _set_status(self, text):
        self.status.config(text=text)

    def _quit(self):
        self._play_stop.set()
        self.link.close()
        self.destroy()


if __name__ == "__main__":
    App(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()
