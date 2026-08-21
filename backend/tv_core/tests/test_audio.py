import asyncio
import os
import struct
import sys

import pytest

from tv_core import audio


def _pcm(*samples):
    return struct.pack(f"<{len(samples)}h", *samples)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# --- level maths ------------------------------------------------------------------------------


def test_full_scale_is_zero_dbfs():
    assert audio.amplitude_to_dbfs(audio.FULL_SCALE) == pytest.approx(0.0)


def test_dbfs_and_amplitude_round_trip():
    for dbfs in (-6, -20, -50, -70):
        assert audio.amplitude_to_dbfs(audio.dbfs_to_amplitude(dbfs)) == pytest.approx(dbfs)


def test_true_silence_reports_the_floor_instead_of_negative_infinity():
    assert audio.amplitude_to_dbfs(0) == audio.SILENT_DBFS
    assert audio.amplitude_to_dbfs(-5) == audio.SILENT_DBFS


def test_a_level_under_the_floor_is_clamped_to_it():
    assert audio.amplitude_to_dbfs(1e-9) == audio.SILENT_DBFS


# --- peak -------------------------------------------------------------------------------------


def test_peak_of_digital_silence_is_zero():
    assert audio.peak(_pcm(0, 0, 0, 0)) == 0


def test_peak_takes_the_largest_magnitude_either_way():
    assert audio.peak(_pcm(100, -3000, 250)) == 3000


def test_peak_handles_the_most_negative_sample_without_overflowing():
    # -32768 has no positive s16 counterpart; abs() of it must not wrap or raise.
    assert audio.peak(_pcm(-32768, 5)) == 32768


def test_peak_ignores_a_trailing_odd_byte_from_a_short_read():
    assert audio.peak(_pcm(1000) + b"\x7f") == 1000


def test_peak_of_an_empty_buffer_is_zero():
    assert audio.peak(b"") == 0


# --- tracker ----------------------------------------------------------------------------------


def test_a_chunk_above_the_threshold_resets_the_silence_clock():
    clock = FakeClock()
    tracker = audio.PeakTracker(-50, clock)
    clock.advance(30)
    assert tracker.silent_seconds() == 30
    tracker.feed(_pcm(int(audio.dbfs_to_amplitude(-20))))
    assert tracker.silent_seconds() == 0


def test_a_chunk_below_the_threshold_leaves_the_clock_running():
    clock = FakeClock()
    tracker = audio.PeakTracker(-50, clock)
    clock.advance(30)
    tracker.feed(_pcm(int(audio.dbfs_to_amplitude(-70))))
    assert tracker.silent_seconds() == 30


def test_a_chunk_exactly_at_the_threshold_counts_as_sound():
    clock = FakeClock()
    tracker = audio.PeakTracker(-50, clock)
    clock.advance(30)
    tracker.feed(_pcm(int(audio.dbfs_to_amplitude(-50)) + 1))
    assert tracker.silent_seconds() == 0


def test_raising_the_floor_makes_a_previously_audible_level_count_as_silence():
    clock = FakeClock()
    tracker = audio.PeakTracker(-70, clock)
    quiet = _pcm(int(audio.dbfs_to_amplitude(-60)))
    tracker.feed(quiet)
    assert tracker.silent_seconds() == 0
    tracker.set_threshold(-50)
    clock.advance(30)
    tracker.feed(quiet)
    assert tracker.silent_seconds() == 30


def test_mark_sound_rearms_the_clock():
    clock = FakeClock()
    tracker = audio.PeakTracker(-50, clock)
    clock.advance(300)
    tracker.mark_sound()
    assert tracker.silent_seconds() == 0


def test_last_peak_is_reported_in_dbfs():
    tracker = audio.PeakTracker(-50, FakeClock())
    tracker.feed(_pcm(int(audio.dbfs_to_amplitude(-20))))
    assert tracker.last_peak_dbfs == pytest.approx(-20, abs=0.01)


# --- environment discovery --------------------------------------------------------------------


def test_pipewire_env_points_at_the_session_holding_the_socket(monkeypatch, tmp_path):
    session = tmp_path / "run" / "user" / "1000"
    session.mkdir(parents=True)
    (session / "pipewire-0").touch()
    monkeypatch.setattr(
        audio, "PIPEWIRE_SOCKET_GLOB", str(tmp_path / "run" / "user" / "*" / "pipewire-0")
    )
    assert audio.pipewire_env()["XDG_RUNTIME_DIR"] == str(session)


def test_pipewire_env_is_none_when_no_session_is_running(monkeypatch, tmp_path):
    monkeypatch.setattr(audio, "PIPEWIRE_SOCKET_GLOB", str(tmp_path / "nothing" / "*"))
    assert audio.pipewire_env() is None


# --- capture command --------------------------------------------------------------------------


def test_capture_command_prefers_pw_record_and_asks_for_the_sink_monitor(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda name: name == "pw-record" or None)
    command = audio.capture_command()
    assert command[0] == "pw-record"
    assert "{ stream.capture.sink=true }" in command


def test_capture_command_falls_back_to_parec_on_the_monitor_never_a_microphone(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda name: name == "parec" or None)
    command = audio.capture_command()
    assert command[0] == "parec"
    assert "--device=@DEFAULT_MONITOR@" in command


def test_capture_command_is_none_when_no_tool_is_installed(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda _name: None)
    assert audio.capture_command() is None


# --- monitor ----------------------------------------------------------------------------------


def test_silent_seconds_is_none_while_nothing_is_being_measured():
    assert audio.SilenceMonitor().silent_seconds() is None


# --- the capture loop, against a stand-in for pw-record ----------------------------------------


async def _shutdown(monitor):
    """Stop the monitor and let the cancelled read loop finish unwinding — it reaps the capture
    process in its cleanup, which the plugin's long-lived loop gets to run for free."""
    monitor.stop()
    await asyncio.sleep(0.05)


def _fake_tool(script):
    return [sys.executable, "-c", script]


def _emit(sample):
    """A tool that writes 400 samples of `sample` and then stays alive, like a real capture."""
    return _fake_tool(
        "import sys, time;"
        f"sys.stdout.buffer.write(__import__('struct').pack('<h', {sample}) * 400);"
        "sys.stdout.flush(); time.sleep(30)"
    )


def _drive(command, monkeypatch, threshold=-50, settle=0.02, rounds=100):
    """Run a SilenceMonitor against `command` until it has read a chunk, then report."""
    monkeypatch.setattr(audio, "pipewire_env", lambda: dict(os.environ))
    monkeypatch.setattr(audio, "capture_command", lambda: command)
    monkeypatch.setattr(audio, "CHUNK_BYTES", 800)

    async def go():
        monitor = audio.SilenceMonitor(threshold)
        monitor.start()
        try:
            for _ in range(rounds):
                await asyncio.sleep(settle)
                if monitor.tracker.last_peak or monitor.unavailable:
                    break
            return monitor.running, monitor.tracker.last_peak, monitor.unavailable
        finally:
            await _shutdown(monitor)

    return asyncio.run(go())


def test_the_monitor_reads_a_level_off_the_capture_tool(monkeypatch):
    running, level, unavailable = _drive(_emit(20000), monkeypatch)
    assert (running, level, unavailable) == (True, 20000, "")


def test_a_capture_of_digital_silence_leaves_the_silence_clock_running(monkeypatch):
    monkeypatch.setattr(audio, "pipewire_env", lambda: dict(os.environ))
    monkeypatch.setattr(audio, "capture_command", lambda: _emit(0))
    monkeypatch.setattr(audio, "CHUNK_BYTES", 800)

    async def go():
        monitor = audio.SilenceMonitor(-50)
        monitor.start()
        try:
            await asyncio.sleep(0.3)
            return monitor.running, monitor.silent_seconds()
        finally:
            await _shutdown(monitor)

    running, silent = asyncio.run(go())
    assert running is True
    assert silent > 0  # zeros never reset the clock, so it keeps counting


def test_a_tool_that_rejects_its_arguments_reports_why_instead_of_retrying_silently(monkeypatch):
    broken = _fake_tool("import sys; sys.stderr.write('unrecognized option --raw\\n'); sys.exit(2)")
    _running, _level, unavailable = _drive(broken, monkeypatch, rounds=50)
    assert "produced no audio" in unavailable
    assert "unrecognized option" in unavailable


def test_a_missing_pipewire_session_is_reported_rather_than_captured(monkeypatch):
    monkeypatch.setattr(audio, "pipewire_env", lambda: None)
    monkeypatch.setattr(audio, "capture_command", lambda: _emit(20000))

    async def go():
        monitor = audio.SilenceMonitor()
        monitor.start()
        try:
            await asyncio.sleep(0.05)
            return monitor.running, monitor.unavailable, monitor.silent_seconds()
        finally:
            await _shutdown(monitor)

    running, unavailable, silent = asyncio.run(go())
    assert running is False
    assert unavailable == "no PipeWire session is running"
    assert silent is None  # "don't know", never mistaken for silence
