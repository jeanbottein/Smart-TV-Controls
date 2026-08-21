"""Tell whether this machine is actually emitting sound — without a microphone.

Reads the audio back off the output sink's *monitor* (a loopback of what is being played, never
a capture device) and reports how long its peak level has stayed under a threshold. Brand-
agnostic: the plugin uses it to decide when to nudge a TV's volume so an ARC soundbar doesn't
fall asleep mid-session.

Amplitude, not device state, on purpose. The cheap signals — /proc/asound's PCM state, a
PipeWire node's RUNNING/IDLE — only say whether a stream is *connected*. PipeWire keeps pushing
digital silence while a game sits at a quiet menu, so those read "active" at exactly the moment
a soundbar has already decided the line is silent and gone to standby.

Levels here are dBFS: decibels relative to digital full scale, so always <= 0. True digital
silence sits below -90, quiet ambience around -60, dialogue above -30. There is no absolute
loudness available on this side of the HDMI cable — a soundbar's own dB scale depends on its
volume knob — so the threshold is calibrated against real gear rather than converted.
"""

import array
import asyncio
import glob
import logging
import math
import os
import shutil
import sys
import time

_logger = logging.getLogger(__name__)

# Where a desktop session's PipeWire socket lives. Globbed rather than hardcoded to uid 1000.
PIPEWIRE_SOCKET_GLOB = "/run/user/*/pipewire-0"

# Only the level matters, so capture as little as possible: 8 kHz mono s16 is 16 KB/s, and a
# half-second window is short enough that a brief sound still lands in a chunk of its own.
SAMPLE_RATE = 8000
CHANNELS = 1
CHUNK_SECONDS = 0.5
CHUNK_BYTES = int(SAMPLE_RATE * CHANNELS * 2 * CHUNK_SECONDS)

FULL_SCALE = 32767
# Reported instead of -inf for a chunk of true digital silence, so callers can format a number.
SILENT_DBFS = -100.0

# Defaults for the stored settings. They live here, next to the measurement they parameterise,
# so main.py and the store can't drift apart on them.
DEFAULT_SILENCE_SECONDS = 240
DEFAULT_SILENCE_DBFS = -50

# How long to wait before respawning a capture that exited. Undocking takes the HDMI sink away,
# which ends the capture; the loop just keeps trying until a sink is back.
RESTART_DELAY = 5


def dbfs_to_amplitude(dbfs):
    """The s16 sample magnitude matching `dbfs` (0 dBFS = full scale)."""
    return FULL_SCALE * (10.0 ** (float(dbfs) / 20.0))


def amplitude_to_dbfs(magnitude):
    """An s16 sample magnitude as dBFS, floored at SILENT_DBFS rather than -inf."""
    if magnitude <= 0:
        return SILENT_DBFS
    return max(SILENT_DBFS, 20.0 * math.log10(magnitude / FULL_SCALE))


def peak(chunk):
    """Largest absolute sample in a little-endian signed 16-bit buffer.

    A trailing odd byte (a short read at EOF) is dropped rather than misread as half a sample.
    """
    usable = len(chunk) - (len(chunk) % 2)
    if usable <= 0:
        return 0
    samples = array.array("h")
    samples.frombytes(bytes(chunk[:usable]))
    if sys.byteorder != "little":
        samples.byteswap()
    # -min rather than abs(): the most negative sample is -32768, which has no positive s16
    # counterpart. Python ints don't overflow, so negating is the correct magnitude.
    return max(max(samples), -min(samples))


class PeakTracker:
    """Turns a stream of PCM chunks into "how long has it been quiet?".

    A chunk counts as sound when its peak reaches the threshold; anything below is silence, the
    same way a soundbar treats a low-level line as nothing at all.
    """

    def __init__(self, threshold_dbfs=DEFAULT_SILENCE_DBFS, clock=time.monotonic):
        self._clock = clock
        self.last_peak = 0
        self.last_sound = clock()
        self.threshold_dbfs = None
        self.set_threshold(threshold_dbfs)

    def set_threshold(self, threshold_dbfs):
        """Re-arm at a new floor. Cheap enough to call on every poll from the stored setting."""
        threshold_dbfs = float(threshold_dbfs)
        if threshold_dbfs == self.threshold_dbfs:
            return
        self.threshold_dbfs = threshold_dbfs
        self._threshold = dbfs_to_amplitude(threshold_dbfs)

    def feed(self, chunk):
        self.last_peak = peak(chunk)
        if self.last_peak >= self._threshold:
            self.last_sound = self._clock()
        return self.last_peak

    def mark_sound(self):
        """Treat now as the last moment sound was heard.

        Used to re-arm after a nudge, when a capture starts (it has heard nothing yet, so the
        gap since the last one isn't measured silence), and on resume — where the process was
        frozen, so any elapsed "silence" is an artifact of sleeping rather than an observation.
        """
        self.last_sound = self._clock()

    def silent_seconds(self):
        return self._clock() - self.last_sound

    @property
    def last_peak_dbfs(self):
        return amplitude_to_dbfs(self.last_peak)


def pipewire_env():
    """Environment for reaching the desktop session's PipeWire, or None if none is up.

    The plugin runs as root (plugin.json sets the _root flag) while PipeWire lives in the
    desktop user's session, so a capture tool needs XDG_RUNTIME_DIR pointed at that session.
    Root bypasses the socket's mode bits, so nothing has to drop privileges to open it.
    """
    for socket in sorted(glob.glob(PIPEWIRE_SOCKET_GLOB)):
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = os.path.dirname(socket)
        return env
    return None


def capture_command():
    """argv that writes the default sink's monitor to stdout as raw s16, or None if no tool.

    Both forms name the *monitor* explicitly — `stream.capture.sink` for pw-record, the
    `@DEFAULT_MONITOR@` pseudo-device for parec — so neither can fall back to a microphone.
    Docked, the default sink is the HDMI one feeding the TV, which is the line we care about.
    """
    if shutil.which("pw-record"):
        return [
            "pw-record",
            "--properties", "{ stream.capture.sink=true }",
            "--rate", str(SAMPLE_RATE),
            "--channels", str(CHANNELS),
            "--format", "s16",
            "--raw",
            "-",
        ]
    if shutil.which("parec"):
        return [
            "parec",
            "--device=@DEFAULT_MONITOR@",
            f"--rate={SAMPLE_RATE}",
            f"--channels={CHANNELS}",
            "--format=s16le",
        ]
    return None


class SilenceMonitor:
    """Keeps a capture running and a PeakTracker fed.

    Every failure answers with "unavailable" rather than raising: no PipeWire session, no
    capture tool installed, or a capture that dies when the sink goes away just means
    `silent_seconds()` returns None until the loop gets it back.

    Note that holding a capture stream on the sink stops PipeWire from suspending it, so the
    ALSA device stays open pushing zeros while this runs. That is why the measurement has no
    gaps — but it is a real change to the machine's audio behaviour, which is why the loop only
    runs while the feature is on and a TV is actually attached.
    """

    def __init__(self, threshold_dbfs=DEFAULT_SILENCE_DBFS):
        self.tracker = PeakTracker(threshold_dbfs)
        self.running = False  # a capture is live and the tracker's numbers are real
        self.unavailable = ""  # why not, for the panel's readout
        self._task = None
        self._process = None

    def configure(self, threshold_dbfs):
        self.tracker.set_threshold(threshold_dbfs)

    def start(self):
        """Idempotent: a second call while the loop is up does nothing."""
        if self._task is not None and not self._task.done():
            return
        _logger.info("audio monitor starting")
        self._task = asyncio.get_event_loop().create_task(self._run())

    def stop(self):
        task, self._task = self._task, None
        if task is None:
            return
        _logger.info("audio monitor stopping")
        task.cancel()
        self._kill()
        self.running = False

    def silent_seconds(self):
        """Seconds the output has been under the threshold, or None while nothing is measured."""
        return self.tracker.silent_seconds() if self.running else None

    def _kill(self):
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass

    async def _run(self):
        last_reason = None
        while True:
            command = capture_command()
            env = pipewire_env()
            if command is None:
                self.unavailable = "no pw-record or parec found on this system"
            elif env is None:
                self.unavailable = "no PipeWire session is running"
            else:
                self.unavailable = ""
                try:
                    await self._capture(command, env)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - never let the loop die on one capture
                    self.unavailable = f"capture failed: {error}"
            if self.unavailable and self.unavailable != last_reason:
                _logger.info("audio monitor unavailable: %s", self.unavailable)
            last_reason = self.unavailable
            await asyncio.sleep(RESTART_DELAY)

    async def _capture(self, command, env):
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as error:
            self.unavailable = f"cannot start {command[0]}: {error}"
            return
        self._process = process
        # A fresh capture has heard nothing yet, so don't count the gap since the last one as
        # measured silence — that would nudge the moment the feature is switched on.
        self.tracker.mark_sound()
        self.running = True
        errors = asyncio.ensure_future(self._tail_stderr(process))
        chunks = 0
        try:
            while True:
                self.tracker.feed(await process.stdout.readexactly(CHUNK_BYTES))
                chunks += 1
        except (asyncio.IncompleteReadError, OSError, ValueError):
            pass  # the capture ended (typically the sink went away); _run respawns it
        finally:
            self.running = False
            self._kill()  # also unblocks the stderr tail, which ends at EOF
            try:
                # Reap it: without this the transport and its pipes survive until GC, and a
                # capture respawns on every undock, so they would pile up over a long session.
                await process.wait()
            except asyncio.CancelledError:
                pass  # stop() cancelled us; the original cancellation still propagates
            except (OSError, ProcessLookupError):
                pass
        if chunks:
            errors.cancel()
            return
        # It never produced any audio at all. That's a rejected argument or a machine with no
        # sink to monitor, not a passing hiccup — say so, rather than respawning silently every
        # RESTART_DELAY while the panel shows "starting…" forever.
        try:
            message = await asyncio.wait_for(errors, 1)
        except asyncio.TimeoutError:
            errors.cancel()
            message = ""
        self.unavailable = f"{command[0]} produced no audio: {message or 'exited immediately'}"

    @staticmethod
    async def _tail_stderr(process):
        """The last of the tool's stderr, read continuously so the pipe can never fill up and
        wedge a capture that would otherwise run for hours."""
        tail = b""
        try:
            while True:
                block = await process.stderr.read(256)
                if not block:
                    break
                tail = (tail + block)[-256:]
        except (OSError, ValueError):
            pass
        return tail.decode("utf-8", "replace").strip()
