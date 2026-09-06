#!/usr/bin/env python3
"""Ghost-player editor — scrub a video and build a movement sequence against it

An animation-style editor for the controller rig. The video sits above a
timeline you can drag through; wherever the playhead lands, the sequence
state at that instant is applied to the rig, so the mechanism always
shows what it would be doing at that frame. Takes are punched in from
the playhead, layered onto what is already there.

    python3 record.py                        # choose everything in-app
    python3 record.py caption.webm           # start with a video open
    python3 record.py caption.webm show.json # and a sequence loaded

TRANSPORT
    click the timeline, or drag the playhead, to scrub
    SPACE   play / stop from the playhead
    R       record a take from the playhead (3-2-1 countdown first)
    L       loop the video
    HOME    jump to the start
    O / J   open a video / open a sequence
    S       save          ESC quit

INPUT
    The panel below is the same control set as control_host.py — two
    joysticks, LT/RT triggers, the two vibration motor faders, the face
    buttons and the d-pad — driven with the mouse. Triggers spring back
    on release; motor faders stay put. A plugged-in gamepad takes over
    if present, and the panel becomes a readout of what it is doing.

    The mouse holds one control at a time, so takes that need two
    channels moving at once are built up in passes — which is what
    punch-in recording below is for.

PUNCH-IN RECORDING
    A take replaces only the channels you actually touched, and only
    across the stretch of time it covers. Everything before and after is
    left alone, and channels you never moved are untouched. So you can
    lay down the sticks over the whole clip, then go back and punch in a
    single button press at 12s without disturbing anything else — the
    behaviour an animation or audio editor gives you, rather than an
    all-or-nothing overwrite.

SEEKING
    Scrubbing decodes one frame at a time on a background thread, always
    for the most recent position asked for and discarding anything
    stale. Dragging therefore stays responsive: the picture trails the
    playhead slightly under a fast drag instead of queueing up every
    intermediate frame and falling behind.

REQUIREMENTS
    pip install pygame pyserial     (pyserial only if driving the rig)
    ffmpeg, for decoding
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time

import pygame

# ---------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------
PORT = "/dev/ttyACM0"       # rig's serial port; ignored if absent
OUTPUT = "recorded.json"    # where S saves
WINDOW = (1280, 860)
VIDEO_H = 470               # video area height; the rest is UI
TIMELINE_H = 54
BAR_H = 34                  # transport button strip
MAX_FPS = 30                # decode cap, as in show.py
DEADZONE = 0.08             # gamepad noise below this reads as centred
HEARTBEAT = 0.4             # keeps the firmware's link failsafe quiet

CHANNELS = ("joyL", "joyR", "trigL", "trigR", "motorA", "motorB",
            "button", "arrow")

NEUTRAL = {"joyL": [0.0, 0.0], "joyR": [0.0, 0.0],
           "trigL": 0.0, "trigR": 0.0,
           "motorA": 0.0, "motorB": 0.0,
           "button": None, "arrow": None}

VIDEO_EXT = (".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v")

BG = (10, 10, 10)
PANEL_BG = (18, 18, 18)
FG = (232, 255, 46)
DIM = (120, 130, 60)
RED = (255, 70, 70)
GREY = (70, 74, 50)


# ---------------------------------------------------------------------
# Rig link (optional — the editor is fully usable with no rig attached)
# ---------------------------------------------------------------------
class Link:
    def __init__(self, port):
        import serial
        self._ser = serial.Serial(port, 115200, timeout=0.1)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # The device sends status lines back. They are ignored, but the
        # buffer still has to be drained or the firmware's own writes
        # eventually block.
        threading.Thread(target=self._drain, daemon=True).start()
        threading.Thread(target=self._beat, daemon=True).start()

    def _drain(self):
        while not self._stop.is_set():
            try:
                self._ser.read(256)
            except Exception:
                return

    def _beat(self):
        while not self._stop.wait(HEARTBEAT):
            self.send({"hb": 1})

    def send(self, obj):
        try:
            with self._lock:
                self._ser.write((json.dumps(obj) + "\n").encode())
        except Exception:
            pass

    def close(self):
        self.send({"stop": 1})
        time.sleep(0.05)
        self._stop.set()
        try:
            self._ser.close()
        except Exception:
            pass


# ---------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------
def ffprobe(path, *args):
    return subprocess.run(["ffprobe", "-v", "error"] + list(args) + [path],
                          capture_output=True, text=True).stdout.strip()


def probe(path):
    """Return (fps, duration_seconds, has_audio)."""
    dims = ffprobe(path, "-select_streams", "v:0", "-show_entries",
                   "stream=r_frame_rate", "-of", "csv=p=0")
    if not dims:
        sys.exit("no video stream found in %s" % path)
    num, _, den = dims.partition("/")
    fps = float(num) / float(den or 1)

    # Container duration is missing from some WebM files, so fall back to
    # counting packets — slower, but it always answers.
    dur = ffprobe(path, "-show_entries", "format=duration", "-of", "csv=p=0")
    if dur in ("", "N/A"):
        dur = ffprobe(path, "-select_streams", "v:0", "-count_packets",
                      "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0")
        duration = int(dur) / fps if dur.isdigit() else 0.0
    else:
        duration = float(dur)

    has_audio = bool(ffprobe(path, "-select_streams", "a:0",
                             "-show_entries", "stream=index", "-of", "csv=p=0"))
    return fps, duration, has_audio


def scale_filter(width, height, fps=None):
    f = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
         "pad=%d:%d:(ow-iw)/2:(oh-ih)/2" % (width, height, width, height))
    return f + (",fps=%.6f" % fps if fps else "")


def open_video(path, width, height, fps, start=0.0):
    """Stream raw RGB frames from `start` seconds onward.

    -ss before -i is an input seek: ffmpeg jumps in the file rather than
    decoding and discarding everything up to that point, which is what
    makes playing from the middle of a long clip instant.
    """
    cmd = ["ffmpeg", "-v", "error"]
    if start > 0:
        cmd += ["-ss", "%.3f" % start]
    cmd += ["-i", path, "-vf", scale_filter(width, height, fps),
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=10 ** 8)


def grab_frame(path, t, width, height):
    """Decode exactly one frame at time t. Returns raw RGB bytes or None."""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "%.3f" % max(0.0, t), "-i", path,
         "-vf", scale_filter(width, height), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True)
    want = width * height * 3
    return r.stdout if len(r.stdout) == want else None


class Seeker:
    """Decodes scrub frames on a background thread.

    Only the most recently requested time is ever worked on; positions
    asked for while a decode is in flight are superseded rather than
    queued. Under a fast drag the picture lags slightly and then catches
    up, instead of grinding through every intermediate frame.
    """

    def __init__(self, path, width, height):
        self.path, self.width, self.height = path, width, height
        self.frame = None            # raw bytes of the last decoded frame
        self._want = None
        self._done = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def request(self, t):
        with self._lock:
            self._want = t
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait(0.1)
            self._wake.clear()
            with self._lock:
                t = self._want
            if t is None or t == self._done:
                continue
            raw = grab_frame(self.path, t, self.width, self.height)
            if raw:
                self.frame = raw
                self._done = t

    def close(self):
        self._stop.set()
        self._wake.set()


def extract_audio(path):
    """Decode the soundtrack to a temporary OGG.

    OGG rather than WAV because pygame's mixer can only reposition
    within compressed formats — and playing from the middle of the clip
    is the whole point of a scrubbing editor.
    """
    ogg = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    ogg.close()
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vn",
                        "-ac", "2", "-ar", "44100", "-c:a", "libvorbis",
                        ogg.name], capture_output=True)
    if r.returncode != 0 or os.path.getsize(ogg.name) == 0:
        os.unlink(ogg.name)
        return None
    return ogg.name


# ---------------------------------------------------------------------
# Control panel widgets — the control_host.py layout, drawn in pygame
# ---------------------------------------------------------------------
class Pad:
    """Circular joystick. Returns [x, y] in -1..1, springs back to centre."""

    def __init__(self, cx, cy, r, label):
        self.cx, self.cy, self.r, self.label = cx, cy, r, label
        self.value = [0.0, 0.0]

    def hit(self, pos):
        return (pos[0] - self.cx) ** 2 + (pos[1] - self.cy) ** 2 <= self.r ** 2

    def drag(self, pos):
        dx = (pos[0] - self.cx) / self.r
        dy = (pos[1] - self.cy) / self.r
        # Clamp to the unit circle, not the unit square: a corner drag
        # must not report a magnitude of 1.41 on the diagonal.
        mag = (dx * dx + dy * dy) ** 0.5
        if mag > 1.0:
            dx, dy = dx / mag, dy / mag
        self.value = [round(dx, 2), round(dy, 2)]

    def release(self):
        self.value = [0.0, 0.0]

    def draw(self, s, font):
        pygame.draw.circle(s, GREY, (self.cx, self.cy), self.r, 2)
        kx = int(self.cx + self.value[0] * (self.r - 16))
        ky = int(self.cy + self.value[1] * (self.r - 16))
        pygame.draw.circle(s, FG, (kx, ky), 14)
        s.blit(font.render(self.label, True, DIM), (self.cx - 6, self.cy - self.r - 18))


class Fader:
    """Vertical 0..1 slider. Spring faders snap back to zero on release."""

    def __init__(self, x, y, w, h, label, spring=False):
        self.rect = pygame.Rect(x, y, w, h)
        self.label, self.spring = label, spring
        self.value = 0.0

    def hit(self, pos):
        return self.rect.collidepoint(pos)

    def drag(self, pos):
        v = 1.0 - (pos[1] - self.rect.y) / self.rect.h
        self.value = round(max(0.0, min(1.0, v)), 2)

    def release(self):
        if self.spring:
            self.value = 0.0

    def draw(self, s, font):
        pygame.draw.rect(s, GREY, self.rect, 1)
        fill = int(self.rect.h * self.value)
        if fill:
            pygame.draw.rect(s, FG, (self.rect.x + 2, self.rect.bottom - fill,
                                     self.rect.w - 4, fill))
        s.blit(font.render(self.label, True, DIM), (self.rect.x - 6, self.rect.bottom + 5))


class Hold:
    """Momentary button. Reports its value only while held down."""

    def __init__(self, x, y, w, h, label, value):
        self.rect = pygame.Rect(x, y, w, h)
        self.label, self.value = label, value
        self.down = False

    def hit(self, pos):
        return self.rect.collidepoint(pos)

    def drag(self, _pos):
        self.down = True

    def release(self):
        self.down = False

    def draw(self, s, font):
        pygame.draw.rect(s, FG, self.rect, 0 if self.down else 2, border_radius=6)
        txt = font.render(self.label, True, BG if self.down else FG)
        s.blit(txt, txt.get_rect(center=self.rect.center))


class Panel:
    def __init__(self, y0, width, height):
        self.rect = pygame.Rect(0, y0, width, height)
        cy = y0 + 100
        self.padL = Pad(190, cy, 78, "L")
        self.padR = Pad(370, cy, 78, "R")
        self.trigL = Fader(40, y0 + 26, 32, 150, "LT", spring=True)
        self.trigR = Fader(478, y0 + 26, 32, 150, "RT", spring=True)
        self.motorA = Fader(556, y0 + 26, 32, 150, "MOT A")
        self.motorB = Fader(624, y0 + 26, 32, 150, "MOT B")
        self.buttons = [Hold(720 + i * 64, y0 + 30, 54, 54, n, n)
                        for i, n in enumerate(("X", "Y", "A", "B"))]
        self.arrows = [Hold(720 + i * 84, y0 + 108, 74, 46, l, v)
                       for i, (l, v) in enumerate(((chr(8592), "left"),
                                                   (chr(8593), "up"),
                                                   (chr(8595), "down"),
                                                   (chr(8594), "right")))]
        self.widgets = ([self.padL, self.padR, self.trigL, self.trigR,
                         self.motorA, self.motorB] + self.buttons + self.arrows)
        self.active = None

    def handle(self, e):
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            if not self.rect.collidepoint(e.pos):
                return
            for w in self.widgets:
                if w.hit(e.pos):
                    self.active = w
                    w.drag(e.pos)
                    break
        elif e.type == pygame.MOUSEMOTION and self.active:
            self.active.drag(e.pos)
        elif e.type == pygame.MOUSEBUTTONUP and e.button == 1 and self.active:
            self.active.release()
            self.active = None

    def read(self):
        return {"joyL": list(self.padL.value), "joyR": list(self.padR.value),
                "trigL": self.trigL.value, "trigR": self.trigR.value,
                "motorA": self.motorA.value, "motorB": self.motorB.value,
                "button": next((b.value for b in self.buttons if b.down), None),
                "arrow": next((a.value for a in self.arrows if a.down), None)}

    def show(self, v):
        """Mirror an external source (gamepad, or the sequence state at
        the playhead) onto the widgets, so the panel always reflects what
        the rig is actually being told to do."""
        self.padL.value = list(v["joyL"])
        self.padR.value = list(v["joyR"])
        self.trigL.value = v["trigL"]
        self.trigR.value = v["trigR"]
        self.motorA.value = v["motorA"]
        self.motorB.value = v["motorB"]
        for b in self.buttons:
            b.down = (b.value == v["button"])
        for a in self.arrows:
            a.down = (a.value == v["arrow"])

    def draw(self, s, font):
        pygame.draw.rect(s, PANEL_BG, self.rect)
        pygame.draw.line(s, GREY, self.rect.topleft, (self.rect.right, self.rect.top))
        for w in self.widgets:
            w.draw(s, font)


# ---------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------
class Timeline:
    """Scrub bar with one lane of event ticks per channel group."""

    LANES = ("joyL", "joyR", "trigL", "trigR", "motorA", "motorB", "button", "arrow")

    def __init__(self, x, y, w, h):
        self.rect = pygame.Rect(x, y, w, h)
        self.duration = 1.0
        self.playhead = 0.0
        self.dragging = False

    def hit(self, pos):
        return self.rect.collidepoint(pos)

    def time_at(self, x):
        f = (x - self.rect.x) / max(1, self.rect.w)
        return max(0.0, min(self.duration, f * self.duration))

    def x_of(self, t):
        return self.rect.x + int(self.rect.w * (t / self.duration if self.duration else 0))

    def handle(self, e):
        """Returns True when the playhead moved, so the caller can seek."""
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and self.hit(e.pos):
            self.dragging = True
            self.playhead = self.time_at(e.pos[0])
            return True
        if e.type == pygame.MOUSEMOTION and self.dragging:
            self.playhead = self.time_at(e.pos[0])
            return True
        if e.type == pygame.MOUSEBUTTONUP and e.button == 1 and self.dragging:
            self.dragging = False
        return False

    def draw(self, s, font, frames, recording_from=None):
        pygame.draw.rect(s, PANEL_BG, self.rect)
        lane_h = max(2, (self.rect.h - 16) // len(self.LANES))

        # A tick per recorded event, on its channel's lane, so the shape
        # of the sequence is readable at a glance.
        for t, d in frames:
            x = self.x_of(t)
            for k in d:
                if k in self.LANES:
                    y = self.rect.y + 4 + self.LANES.index(k) * lane_h
                    pygame.draw.line(s, DIM, (x, y), (x, y + lane_h - 1))

        # Punch-in region currently being recorded.
        if recording_from is not None:
            x0, x1 = self.x_of(recording_from), self.x_of(self.playhead)
            band = pygame.Surface((max(1, x1 - x0), self.rect.h), pygame.SRCALPHA)
            band.fill((255, 70, 70, 60))
            s.blit(band, (x0, self.rect.y))

        px = self.x_of(self.playhead)
        pygame.draw.line(s, FG, (px, self.rect.y), (px, self.rect.bottom), 2)
        pygame.draw.polygon(s, FG, [(px - 5, self.rect.y), (px + 5, self.rect.y),
                                    (px, self.rect.y + 7)])
        s.blit(font.render("%05.2f / %05.2f s" % (self.playhead, self.duration),
                           True, DIM), (self.rect.x + 4, self.rect.bottom - 15))


class Button:
    """Transport button: a label, a rectangle and a callback."""

    def __init__(self, x, y, w, label, action, toggle=False):
        self.rect = pygame.Rect(x, y, w, BAR_H - 8)
        self.label, self.action, self.toggle = label, action, toggle
        self.on = False

    def handle(self, e):
        if (e.type == pygame.MOUSEBUTTONDOWN and e.button == 1
                and self.rect.collidepoint(e.pos)):
            self.action()
            return True
        return False

    def draw(self, s, font):
        pygame.draw.rect(s, FG if self.on else GREY, self.rect, 0 if self.on else 1,
                         border_radius=4)
        txt = font.render(self.label, True, BG if self.on else FG)
        s.blit(txt, txt.get_rect(center=self.rect.center))


# ---------------------------------------------------------------------
# Gamepad — used instead of the panel when one is plugged in
# ---------------------------------------------------------------------
class Gamepad:
    """Axis numbering is not standardised across drivers. This assumes
    the common Linux layout: sticks on 0/1 and 3/4, triggers on 2/5."""

    def __init__(self, pad):
        self.pad = pad
        self.name = pad.get_name()

    @staticmethod
    def _dz(v):
        return 0.0 if abs(v) < DEADZONE else round(v, 2)

    def read(self):
        p = self.pad
        n = p.get_numaxes()

        def ax(i):
            return self._dz(p.get_axis(i)) if i < n else 0.0

        def trig(i):
            # Triggers rest at -1 and travel to +1; remap onto 0..1.
            return round(max(0.0, (p.get_axis(i) + 1) / 2), 2) if i < n else 0.0

        btn = None
        for i, name in ((0, "A"), (1, "B"), (2, "X"), (3, "Y")):
            if i < p.get_numbuttons() and p.get_button(i):
                btn = name
                break

        arrow = None
        if p.get_numhats():
            hx, hy = p.get_hat(0)
            arrow = ("left" if hx < 0 else "right" if hx > 0 else
                     "up" if hy > 0 else "down" if hy < 0 else None)

        nb = p.get_numbuttons()
        return {"joyL": [ax(0), ax(1)], "joyR": [ax(3), ax(4)],
                "trigL": trig(2), "trigR": trig(5),
                "motorA": 1.0 if (nb > 4 and p.get_button(4)) else 0.0,
                "motorB": 1.0 if (nb > 5 and p.get_button(5)) else 0.0,
                "button": btn, "arrow": arrow}


def find_gamepad():
    pygame.joystick.init()
    if not pygame.joystick.get_count():
        return None
    pad = pygame.joystick.Joystick(0)
    pad.init()
    return Gamepad(pad)


# ---------------------------------------------------------------------
# Sequence handling
# ---------------------------------------------------------------------
def state_at(frames, t):
    """Fold every delta up to time t into a full channel state."""
    s = dict(NEUTRAL)
    for ft, d in frames:
        if ft > t:
            break
        s.update(d)
    return s


def punch_in(old, new, t0, t1):
    """Merge a take into an existing sequence.

    Only the channels the take actually touched are replaced, and only
    between t0 and t1. Data outside that window, and channels never
    moved, survive untouched — so a take can be dropped into the middle
    of a sequence without disturbing what surrounds it.
    """
    touched = {k for _, d in new for k in d}
    kept = []
    for t, d in old:
        trimmed = ({k: v for k, v in d.items() if k not in touched}
                   if t0 <= t <= t1 else dict(d))
        if trimmed:
            kept.append([t, trimmed])
    return sorted(kept + new, key=lambda f: f[0])


def load_sequence(path):
    with open(path) as f:
        return [[float(t), d] for t, d in json.load(f)["frames"]]


def save_sequence(path, frames):
    with open(path, "w") as f:
        json.dump({"version": 1, "channels": list(CHANNELS),
                   "duration": frames[-1][0] if frames else 0.0,
                   "frames": frames}, f, indent=1)


# ---------------------------------------------------------------------
# In-app file picker
# ---------------------------------------------------------------------
def pick_file(screen, font, exts, title):
    """Modal list of matching files in the working directory.

    Returns a filename, or None if cancelled — deliberately not a system
    file dialog, which would drag in Tk purely to choose a file.
    """
    files = sorted(f for f in os.listdir(".") if f.lower().endswith(exts))
    if not files:
        return None
    sel = 0
    while True:
        screen.fill(BG)
        screen.blit(font.render(title + "   (arrows, enter, esc)", True, FG), (40, 40))
        for i, f in enumerate(files[:24]):
            screen.blit(font.render(("> " if i == sel else "  ") + f, True,
                                    FG if i == sel else DIM), (40, 84 + i * 26))
        pygame.display.flip()
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                return None
            if e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    return None
                if e.key == pygame.K_DOWN:
                    sel = (sel + 1) % len(files)
                if e.key == pygame.K_UP:
                    sel = (sel - 1) % len(files)
                if e.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    return files[sel]
        time.sleep(0.01)


# ---------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------
class Editor:
    def __init__(self, video=None, sequence=None):
        pygame.init()
        self.screen = pygame.display.set_mode(WINDOW)
        pygame.display.set_caption("ghost-player editor")
        self.font = pygame.font.Font(None, 21)
        self.big = pygame.font.Font(None, 32)
        self.width = self.screen.get_width()

        y = VIDEO_H
        self.timeline = Timeline(0, y, self.width, TIMELINE_H)
        y += TIMELINE_H
        self.bar_y = y
        y += BAR_H
        self.panel = Panel(y, self.width, WINDOW[1] - y)

        self.buttons = []
        x = 8
        for label, action, toggle in (
                ("open video", self.open_video, False),
                ("open seq", self.open_sequence, False),
                ("save", self.save, False),
                ("play", self.toggle_play, False),
                ("rec", self.toggle_record, False),
                ("loop", self.toggle_loop, True),
                ("start", self.to_start, False)):
            b = Button(x, y - BAR_H + 4, 92, label, action, toggle)
            self.buttons.append(b)
            x += 96
        self.btn_play, self.btn_rec, self.btn_loop = self.buttons[3:6]

        self.gamepad = find_gamepad()
        self.panel_enabled = self.gamepad is None

        self.video = None
        self.seeker = None
        self.audio = None
        self.fps = 30.0
        self.frames = load_sequence(sequence) if sequence else []
        self.seq_path = sequence
        self.mode = "idle"          # idle | play | rec
        self.proc = None
        self.take = []
        self.take_start = 0.0
        self.sent = dict(NEUTRAL)
        self.index = 0
        self.t0 = 0.0
        self.playhead_at_start = 0.0
        self.play_cursor = 0        # next sequence frame due during play
        self.frame_surface = None
        self.note = "open a video to begin"

        try:
            self.link = Link(PORT)
        except Exception as e:
            self.link = None
            print("no rig on %s (%s) — editing only" % (PORT, e))

        if video:
            self.load_video(video)

    # -- media --------------------------------------------------------
    def load_video(self, path):
        if not os.path.exists(path):
            self.note = "not found: %s" % path
            return
        self.stop()
        if self.seeker:
            self.seeker.close()
        if self.audio:
            pygame.mixer.music.stop()
            os.unlink(self.audio)
            self.audio = None

        self.video = path
        src_fps, duration, has_audio = probe(path)
        self.fps = min(src_fps, MAX_FPS) if MAX_FPS else src_fps
        # The timeline spans whichever is longer: a sequence may run past
        # the end of the clip it was written against, and that data must
        # stay reachable rather than being cropped out of view.
        self.timeline.duration = max(duration, self.seq_duration(), 1.0)
        self.seeker = Seeker(path, self.width, VIDEO_H)
        if has_audio:
            self.audio = extract_audio(path)
            if self.audio:
                if not pygame.mixer.get_init():
                    pygame.mixer.init(frequency=44100)
                pygame.mixer.music.load(self.audio)
        self.seek(self.timeline.playhead)
        self.note = "%s  ·  %.1fs  ·  %.2f fps" % (os.path.basename(path),
                                                   duration, self.fps)

    def seq_duration(self):
        return self.frames[-1][0] if self.frames else 0.0

    def open_video(self):
        f = pick_file(self.screen, self.font, VIDEO_EXT, "open video")
        if f:
            self.load_video(f)

    def open_sequence(self):
        f = pick_file(self.screen, self.font, (".json",), "open sequence")
        if not f:
            return
        try:
            self.frames = load_sequence(f)
        except Exception as e:
            self.note = "bad sequence: %s" % e
            return
        self.seq_path = f
        self.timeline.duration = max(self.timeline.duration, self.seq_duration(), 1.0)
        self.apply_state()
        self.note = "loaded %s (%d frames)" % (f, len(self.frames))

    def save(self):
        if not self.frames:
            self.note = "nothing to save"
            return
        save_sequence(OUTPUT, self.frames)
        self.note = "saved %s (%d frames)" % (OUTPUT, len(self.frames))
        print(self.note)

    # -- transport ----------------------------------------------------
    def seek(self, t):
        self.timeline.playhead = max(0.0, min(self.timeline.duration, t))
        if self.seeker:
            self.seeker.request(self.timeline.playhead)
        self.apply_state()

    def apply_state(self):
        """Push the sequence state at the playhead to panel and rig."""
        s = state_at(self.frames, self.timeline.playhead)
        if self.mode != "rec":
            self.panel.show(s)
        if self.link and s != self.sent:
            self.link.send(s)
            self.sent = dict(s)

    def to_start(self):
        self.stop()
        self.seek(0.0)

    def toggle_loop(self):
        self.btn_loop.on = not self.btn_loop.on

    def toggle_play(self):
        self.stop() if self.mode == "play" else self.start("play")

    def toggle_record(self):
        self.stop() if self.mode == "rec" else self.start("rec")

    def start(self, mode):
        if not self.video:
            self.note = "open a video first"
            return
        self.stop()
        if mode == "rec" and not self.countdown():
            return
        self.playhead_at_start = self.timeline.playhead
        self.take_start = self.timeline.playhead
        self.take = []
        self.sent = dict(NEUTRAL)
        self.index = 0
        # Sequence events before the playhead have already been folded in
        # by apply_state(); playback resumes from the first one after it.
        self.play_cursor = sum(1 for t, _ in self.frames if t <= self.timeline.playhead)
        self.proc = open_video(self.video, self.width, VIDEO_H, self.fps,
                               self.timeline.playhead)
        if self.audio:
            pygame.mixer.music.play()
            try:
                pygame.mixer.music.set_pos(self.timeline.playhead)
            except pygame.error:
                pass          # some builds refuse mid-file starts; carry on muted
        self.t0 = time.monotonic()
        self.mode = mode
        self.btn_play.on = (mode == "play")
        self.btn_rec.on = (mode == "rec")
        self.note = ""

    def stop(self):
        if self.mode == "rec" and self.take:
            self.frames = punch_in(self.frames, self.take,
                                   self.take_start, self.timeline.playhead)
            self.timeline.duration = max(self.timeline.duration, self.seq_duration())
            self.note = "punched in %.2f–%.2fs (%d frames total)" % (
                self.take_start, self.timeline.playhead, len(self.frames))
        self.take = []
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc.stdout.close()
            self.proc = None
        if self.audio:
            pygame.mixer.music.stop()
        self.mode = "idle"
        self.btn_play.on = self.btn_rec.on = False
        if self.link:
            self.link.send(NEUTRAL)
            self.sent = dict(NEUTRAL)
        self.apply_state()

    def countdown(self):
        """3-2-1 before a take, so the operator can get their hands ready."""
        huge = pygame.font.Font(None, 200)
        for n in ("3", "2", "1"):
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.0:
                for e in pygame.event.get():
                    if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE:
                        return False
                pygame.draw.rect(self.screen, BG, (0, 0, self.width, VIDEO_H))
                surf = huge.render(n, True, FG)
                self.screen.blit(surf, surf.get_rect(center=(self.width // 2,
                                                             VIDEO_H // 2)))
                self.draw_chrome()
                pygame.display.flip()
                time.sleep(0.02)
        return True

    # -- main loop ----------------------------------------------------
    def run(self):
        try:
            running = True
            while running:
                for e in pygame.event.get():
                    if e.type == pygame.QUIT:
                        running = False
                    elif e.type == pygame.KEYDOWN:
                        running = self.on_key(e.key)
                    else:
                        if any(b.handle(e) for b in self.buttons):
                            continue
                        if self.timeline.handle(e) and self.mode == "idle":
                            self.seek(self.timeline.playhead)
                        elif self.panel_enabled:
                            self.panel.handle(e)

                if self.mode in ("play", "rec"):
                    self.advance()
                elif self.seeker and self.seeker.frame:
                    self.frame_surface = pygame.image.frombuffer(
                        self.seeker.frame, (self.width, VIDEO_H), "RGB")

                self.draw()
                if self.mode == "idle":
                    time.sleep(0.01)      # idle UI does not need a busy loop
        finally:
            self.stop()
            if self.seeker:
                self.seeker.close()
            if self.link:
                self.link.close()
            pygame.quit()
            if self.audio:
                os.unlink(self.audio)

    def on_key(self, key):
        if key == pygame.K_ESCAPE:
            return False
        if key == pygame.K_SPACE:
            self.toggle_play()
        elif key == pygame.K_r:
            self.toggle_record()
        elif key == pygame.K_l:
            self.toggle_loop()
        elif key == pygame.K_s and self.mode == "idle":
            self.save()
        elif key == pygame.K_o and self.mode == "idle":
            self.open_video()
        elif key == pygame.K_j and self.mode == "idle":
            self.open_sequence()
        elif key == pygame.K_HOME:
            self.to_start()
        return True

    def advance(self):
        raw = self.proc.stdout.read(self.width * VIDEO_H * 3)
        if len(raw) < self.width * VIDEO_H * 3:          # end of clip
            looping = self.btn_loop.on and self.mode == "play"
            mode = self.mode
            self.stop()
            if looping:
                self.seek(0.0)
                self.start(mode)
            else:
                self.note = "reached the end"
            return

        self.index += 1
        elapsed = time.monotonic() - self.t0

        # Present each frame at its own timestamp, dropping it if we are
        # late: the timeline must follow the video's clock, not the
        # decoder's pace.
        due = self.index / self.fps
        late = elapsed - due
        if late < 0:
            time.sleep(-late)
            elapsed = time.monotonic() - self.t0
        if late <= 1.0 / self.fps:
            self.frame_surface = pygame.image.frombuffer(raw, (self.width, VIDEO_H), "RGB")
        self.timeline.playhead = min(self.playhead_at_start + elapsed,
                                     self.timeline.duration)

        # Replay the existing sequence as we go, so a take is performed
        # against what is already there.
        while (self.play_cursor < len(self.frames)
               and self.frames[self.play_cursor][0] <= self.timeline.playhead):
            d = self.frames[self.play_cursor][1]
            if self.link:
                self.link.send(d)
            if self.mode == "play":
                self.sent.update(d)
                self.panel.show(state_at(self.frames, self.timeline.playhead))
            self.play_cursor += 1

        if self.mode == "rec":
            now = self.gamepad.read() if self.gamepad else self.panel.read()
            if self.gamepad:
                self.panel.show(now)
            # Capture only what changed: a sequence of deltas stays small
            # and readable, and matches what show.py expects.
            delta = {k: v for k, v in now.items() if v != self.sent[k]}
            if delta:
                self.take.append([round(self.timeline.playhead, 4), dict(delta)])
                self.sent.update(delta)
                if self.link:
                    self.link.send(delta)

    # -- drawing ------------------------------------------------------
    def draw(self):
        if self.frame_surface:
            self.screen.blit(self.frame_surface, (0, 0))
        else:
            pygame.draw.rect(self.screen, BG, (0, 0, self.width, VIDEO_H))
        self.draw_chrome()
        pygame.display.flip()

    def draw_chrome(self):
        self.timeline.draw(self.screen, self.font, self.frames,
                           self.take_start if self.mode == "rec" else None)
        pygame.draw.rect(self.screen, BG, (0, self.bar_y, self.width, BAR_H))
        for b in self.buttons:
            b.draw(self.screen, self.font)
        self.panel.draw(self.screen, self.font)

        label = {"idle": "IDLE", "play": "PLAY", "rec": "REC"}[self.mode]
        self.screen.blit(self.big.render(label, True,
                                         RED if self.mode == "rec" else FG), (16, 12))
        info = "%d frames   %s" % (len(self.frames),
                                   self.gamepad.name if self.gamepad else "panel")
        self.screen.blit(self.font.render(info, True, FG), (100, 20))
        if self.note:
            self.screen.blit(self.font.render(self.note, True, DIM), (16, 44))


def main():
    video = sys.argv[1] if len(sys.argv) > 1 else None
    sequence = sys.argv[2] if len(sys.argv) > 2 else None
    Editor(video, sequence).run()


if __name__ == "__main__":
    main()
