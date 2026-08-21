#!/usr/bin/env python3
"""Standalone output-level probe. Usage: python3 scripts/audio_probe.py [SECONDS]

Prints the peak level of whatever this machine is playing, once a second, so the Audio
keep-alive's silence floor can be picked against real hardware rather than guessed:

  1. play nothing and note the peak with the room quiet — that's the noise floor
  2. play something you'd consider "the TV is in use" and note that peak
  3. set the floor in the panel between the two

Levels are dBFS (decibels below digital full scale, so always negative). Reads the output
sink's *monitor* — a loopback of what is being played — so no microphone is involved.
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from tv_core import audio

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 0  # 0 = until Ctrl-C


async def main():
    command = audio.capture_command()
    env = audio.pipewire_env()
    if command is None:
        sys.exit("no pw-record or parec found — install pipewire-utils or pulseaudio-utils")
    if env is None:
        sys.exit(f"no PipeWire session socket matched {audio.PIPEWIRE_SOCKET_GLOB}")
    print(f"capturing via {command[0]} (XDG_RUNTIME_DIR={env['XDG_RUNTIME_DIR']})\n")

    monitor = audio.SilenceMonitor()
    monitor.start()
    elapsed = 0.0
    try:
        while not DURATION or elapsed < DURATION:
            await asyncio.sleep(1)
            elapsed += 1
            if not monitor.running:
                print(f"  waiting for a capture… {monitor.unavailable}")
                continue
            peak = monitor.tracker.last_peak_dbfs
            # A bar makes the noise floor and real content obvious at a glance.
            bar = "#" * max(0, int((peak + 90) / 3))
            print(f"  peak {peak:7.1f} dBFS  quiet {monitor.silent_seconds():5.0f}s  {bar}")
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())
