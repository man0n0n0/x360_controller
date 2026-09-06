#!/usr/bin/env python3
"""Ghost-player show — full-screen video with a synchronised controller sequence

Plays a video full screen and, on the same clock, replays a recorded
movement sequence on the controller rig over USB. Made for unattended
installation use: it starts, it runs, it leaves the rig safe when it
ends.

    python3 show.py                                   # uses the settings below
    python3 show.py film.mp4 sequence.json            # or name them
    python3 show.py film.mp4 sequence.json /dev/ttyACM0

Press ESC or Q at any time to stop.

Sequence files are the ones recorded by control_host.py — same format,
no conversion step.

REQUIREMENTS
    pip install pygame pyserial
    ffmpeg (with ffprobe) on the PATH — `sudo apt install ffmpeg`

WHY FFMPEG AND NOT A PYTHON VIDEO LIBRARY
    pygame cannot decode video on its own. The usual alternative,
    opencv-python, is a ~90 MB dependency whose only job here would be
    decoding a file, and it still cannot play the soundtrack. ffmpeg is
    already present on virtually every Linux machine, decodes anything,
    and scales the picture to the screen for free — so it does the
    decoding and pygame does nothing but display finished frames.

HOW SYNC WORKS
    One monotonic clock is started the moment the first frame is shown.
    Both the video and the sequence are scheduled against it
    independently, rather than one being driven off the other. Neither
    can therefore drag the other out of time: if the machine is briefly
    too slow, video frames are dropped to catch up while the controller
    keeps its own schedule, and both land back in step.

SAFETY
    The rig is commanded to neutral when the show ends, when ESC is
    pressed, and if this program crashes. The firmware also runs its own
    failsafe: it zeroes every output if it hears nothing for 1.5 s, so a
    host that dies mid-show cannot leave a trigger held down. The
    heartbeat below exists to keep that failsafe quiet during the long
    still passages of a sequence.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time

# ---------------------------------------------------------------------
# Settings — edit these for the installation, or pass them on the
# command line, which overrides them.
# ---------------------------------------------------------------------
VIDEO = "caption.webm"          # video file to play
SEQUENCE = "show.json"      # sequence recorded with control_host.py
PORT = "/dev/ttyACM0"       # USB serial port of the controller

FULLSCREEN = True           # False gives a window, useful while setting up
LOOP = False                # True restarts video and sequence together
MAX_FPS = 30                # cap the decoded frame rate; 0 = play at source rate
HEARTBEAT = 0.4             # seconds; must stay well under the 1.5 s failsafe

# MAX_FPS matters on small machines. Frames cross from ffmpeg to pygame
# as raw RGB, so the pipe carries width x height x 3 bytes per frame:
# 1080p at 60 fps is roughly 370 MB/s, which a desktop absorbs and a
# Raspberry Pi does not. Halving the rate halves that traffic, and it
# also halves the number of blits Python has to perform. The scheduler
# below drops frames rather than running late, so a source rate the
# machine cannot sustain looks *worse* than a cap it can hold: capped
# playback is smooth, uncapped playback stutters. Set 0 to disable.

NEUTRAL = {"joyL": [0.0, 0.0], "joyR": [0.0, 0.0],
           "trigL": 0.0, "trigR": 0.0,
           "motorA": 0.0, "motorB": 0.0,
           "button": None, "arrow": None}


# ---------------------------------------------------------------------
# Controller link — newline-delimited JSON over USB serial.
#
# Deliberately a self-contained copy rather than an import from
# control_host.py: that module pulls in Tk, and this program is meant to
# run on an installation machine that may have nothing but pygame
# installed. Thirty lines of duplication buys one file with no desktop
# dependencies.
# ---------------------------------------------------------------------
class Link:
    def __init__(self, port):
        import serial
        # Baud rate is a formality: the C3's port is native USB CDC, not
        # a UART behind a bridge chip, so the line settings are
        # negotiated away and frames move at USB speed regardless.
        self._ser = serial.Serial(port, 115200, timeout=0.1)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # The device talks back (status events, calibration notices). It
        # is all ignored here, but the buffer must still be drained: an
        # unread port eventually blocks the firmware's own writes.
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        while not self._stop.is_set():
            try:
                self._ser.read(256)
            except Exception:
                return

    def send(self, obj):
        try:
            with self._lock:
                self._ser.write((json.dumps(obj) + "\n").encode())
        except Exception as e:
            print("serial write failed:", e, file=sys.stderr)

    def close(self):
        self.send({"stop": 1})             # leave the rig safe
        time.sleep(0.05)                   # let it reach the device
        self._stop.set()
        try:
            self._ser.close()
        except Exception:
            pass


# ---------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------
def probe(path):
    """Return (width, height, fps, has_audio) for a video file."""
    def ff(*args):
        return subprocess.run(["ffprobe", "-v", "error"] + list(args) + [path],
                              capture_output=True, text=True).stdout.strip()

    dims = ff("-select_streams", "v:0", "-show_entries",
              "stream=width,height,r_frame_rate", "-of", "csv=p=0")
    if not dims:
        sys.exit("no video stream found in %s" % path)
    w, h, rate = dims.split(",")[:3]
    # ffprobe reports frame rate as a fraction, e.g. "30000/1001".
    num, _, den = rate.partition("/")
    fps = float(num) / float(den or 1)
    has_audio = bool(ff("-select_streams", "a:0",
                        "-show_entries", "stream=index", "-of", "csv=p=0"))
    return int(w), int(h), fps, has_audio


def open_video(path, width, height, fps):
    """Start ffmpeg decoding to raw RGB frames on stdout.

    The picture is scaled to the screen and letterboxed by ffmpeg, so
    every frame arrives exactly screen-sized and pygame can blit it
    straight to (0, 0) with no per-frame scaling work.

    `fps` is the rate frames actually leave ffmpeg at, already capped by
    MAX_FPS. The fps filter does the dropping inside ffmpeg, which is
    far cheaper than shipping frames down the pipe for Python to discard.
    """
    scale = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
             "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,fps=%.6f"
             % (width, height, width, height, fps))
    # stderr is discarded on purpose. Stopping the show closes this pipe
    # while ffmpeg is still writing to it, and the resulting broken-pipe
    # complaints would print over the customer's console every time. A
    # genuinely unplayable file is caught by probe() before we get here.
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", scale,
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10 ** 8)


def extract_audio(path):
    """Decode the soundtrack to a temporary WAV, or None if there is no
    audio track. Extracted up front rather than streamed: pygame's mixer
    wants a file, and one pass over the audio costs a second or two at
    startup instead of risking a stall mid-show."""
    wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav.close()
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path,
                        "-vn", "-ac", "2", "-ar", "44100", wav.name],
                       capture_output=True)
    if r.returncode != 0 or os.path.getsize(wav.name) == 0:
        os.unlink(wav.name)
        return None
    return wav.name


# ---------------------------------------------------------------------
# Sequence playback
# ---------------------------------------------------------------------
def load_sequence(path):
    with open(path) as f:
        return [[float(t), d] for t, d in json.load(f)["frames"]]


def play_sequence(link, frames, clock, stop):
    """Send each recorded delta at its recorded time.

    Runs on its own thread. Waits are computed against the shared show
    clock rather than by adding up gaps, so a late send never pushes
    everything after it later still.
    """
    for t, delta in frames:
        wait = t - clock()
        if wait > 0:
            # Waking on the stop event rather than sleeping blind means
            # ESC takes effect immediately, even during a long still.
            if stop.wait(wait):
                return
        elif stop.is_set():
            return
        link.send(delta)
    link.send(NEUTRAL)


def heartbeat(link, stop):
    """Keep the firmware's link failsafe quiet while the show runs."""
    while not stop.wait(HEARTBEAT):
        link.send({"hb": 1})


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main():
    video = sys.argv[1] if len(sys.argv) > 1 else VIDEO
    sequence = sys.argv[2] if len(sys.argv) > 2 else SEQUENCE
    port = sys.argv[3] if len(sys.argv) > 3 else PORT

    for f in (video, sequence):
        if not os.path.exists(f):
            sys.exit("file not found: %s" % f)

    try:
        import pygame
    except ImportError:
        sys.exit("pygame is required:  pip install pygame")

    frames = load_sequence(sequence)
    link = Link(port)

    pygame.init()
    pygame.mouse.set_visible(False)
    # (0, 0) asks for the current desktop resolution. No SCALED flag:
    # ffmpeg already delivers frames at exactly this size, so there is
    # nothing for pygame to rescale — and SCALED rejects a zero size.
    screen = pygame.display.set_mode((0, 0) if FULLSCREEN else (1280, 720),
                                     pygame.FULLSCREEN if FULLSCREEN else 0)
    pygame.display.set_caption("ghost-player")
    width, height = screen.get_size()

    # Black holding screen: extracting the audio takes a moment on a
    # long film, and a customer should never see a desktop behind it.
    screen.fill((0, 0, 0))
    pygame.display.flip()

    _, _, src_fps, has_audio = probe(video)
    # One rate is used for both the decode filter and the presentation
    # schedule below, so the two can never disagree about when a given
    # frame is due.
    fps = min(src_fps, MAX_FPS) if MAX_FPS else src_fps
    audio = extract_audio(video) if has_audio else None
    if audio:
        pygame.mixer.init(frequency=44100)
        pygame.mixer.music.load(audio)

    stop = threading.Event()
    frame_bytes = width * height * 3
    threads = []

    try:
        while True:
            proc = open_video(video, width, height, fps)
            if audio:
                pygame.mixer.music.play()
            t0 = time.monotonic()
            clock = lambda: time.monotonic() - t0

            # Started per run so a looping show restarts the movements
            # in step with the picture.
            threads = [threading.Thread(target=play_sequence,
                                        args=(link, frames, clock, stop), daemon=True),
                       threading.Thread(target=heartbeat,
                                        args=(link, stop), daemon=True)]
            for t in threads:
                t.start()

            index = 0
            while not stop.is_set():
                raw = proc.stdout.read(frame_bytes)
                if len(raw) < frame_bytes:
                    break                       # end of file
                index += 1

                for e in pygame.event.get():
                    if e.type == pygame.QUIT or (
                            e.type == pygame.KEYDOWN and
                            e.key in (pygame.K_ESCAPE, pygame.K_q)):
                        stop.set()

                # Present this frame at its own timestamp. If the machine
                # has fallen more than a frame behind, skip the blit and
                # move on: dropping a frame is invisible, while showing
                # every frame late would let the picture drift out of
                # sync with the controller for good.
                due = index / fps
                late = clock() - due
                if late > 1.0 / fps:
                    continue
                if late < 0:
                    time.sleep(-late)

                surf = pygame.image.frombuffer(raw, (width, height), "RGB")
                screen.blit(surf, (0, 0))
                pygame.display.flip()

            # Stop the decoder and let it exit before closing the pipe:
            # closing first leaves ffmpeg writing into a dead pipe.
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            proc.stdout.close()
            if audio:
                pygame.mixer.music.stop()

            if stop.is_set() or not LOOP:
                break
            stop.set()                          # retire this run's threads
            for t in threads:
                t.join(timeout=1)
            stop.clear()

    finally:
        # Runs on ESC, on a normal finish, and on any crash — the rig
        # must never be left holding an input.
        stop.set()
        for t in threads:
            t.join(timeout=1)
        link.close()
        pygame.quit()
        if audio:
            os.unlink(audio)


if __name__ == "__main__":
    main()
